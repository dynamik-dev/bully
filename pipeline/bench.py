"""
Bully Test Bench

Two modes:
  bully bench                         -- run fixture suite, append to bench/history.jsonl
  bully bench --config <path>         -- analyze token cost of any .bully.yml

Stdlib-only except for the optional `anthropic` import, which is gated
behind API-key presence and falls back to a char-count proxy.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


class FixtureError(Exception):
    """Raised when a fixture directory is malformed."""


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    file_path: str
    edit_type: str
    diff: str
    config_path: Path

    @property
    def dir(self) -> Path:
        return self.config_path.parent


def load_fixture(fixture_dir: Path) -> Fixture:
    """Load a fixture from `<dir>/config.yml` + `<dir>/fixture.json`."""
    fixture_dir = Path(fixture_dir)
    cfg = fixture_dir / "config.yml"
    meta = fixture_dir / "fixture.json"
    if not cfg.is_file():
        raise FixtureError(f"missing config.yml in {fixture_dir}")
    if not meta.is_file():
        raise FixtureError(f"missing fixture.json in {fixture_dir}")
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FixtureError(f"malformed fixture.json in {fixture_dir}: {e}") from e

    required = ("name", "description", "file_path", "edit_type", "diff")
    for key in required:
        if key not in data:
            raise FixtureError(f"fixture.json in {fixture_dir} missing field {key!r}")

    return Fixture(
        name=data["name"],
        description=data["description"],
        file_path=data["file_path"],
        edit_type=data["edit_type"],
        diff=data["diff"],
        config_path=cfg,
    )


def discover_fixtures(root: Path) -> list[Fixture]:
    """Load every fixture subdirectory under `root`, sorted by name."""
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[Fixture] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        out.append(load_fixture(child))
    return out


BENCH_MODEL = "claude-sonnet-4-6"


def _repo_root() -> Path:
    """Return the project root (directory holding the `agents/` dir).

    Assumes bench.py lives at <root>/pipeline/bench.py.
    """
    return Path(__file__).resolve().parent.parent


def _import_anthropic():
    """Import and return the anthropic module, or None if unavailable."""
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return None
    return anthropic


def load_evaluator_system_prompt() -> str:
    """Load the bully-evaluator system prompt from agents/bully-evaluator.md.

    Strips the YAML frontmatter (everything between the first `---` pair).
    """
    path = _repo_root() / "agents" / "bully-evaluator.md"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        # Find the closing frontmatter delimiter.
        rest = text[3:]
        end = rest.find("\n---")
        if end != -1:
            text = rest[end + 4 :]  # past "\n---"
    return text.lstrip("\n")


def count_tokens(payload: dict, *, system: str, use_api: bool = True) -> tuple[int, str]:
    """Count input tokens for the given bully-evaluator payload.

    Returns (token_count, method) where method is 'count_tokens' or 'proxy'.

    Uses the Anthropic `messages/count_tokens` endpoint when
    ANTHROPIC_API_KEY is set AND the anthropic SDK is importable AND
    use_api is True. Falls back to `len(json.dumps(payload)) + len(system)`.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    anthropic = _import_anthropic() if use_api else None
    if use_api and api_key and anthropic is not None:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.count_tokens(
                model=BENCH_MODEL,
                system=system,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            return int(resp.input_tokens), "count_tokens"
        except Exception:
            # Any API failure -> proxy. Bench must not crash on transient errors.
            pass
    return len(json.dumps(payload)) + len(system), "proxy"


class PhaseTimer:
    """Callable that records elapsed time for each named phase.

    Usage:
        pt = PhaseTimer()
        with pt("parse_config"):
            ...
        pt.results_ns()  # {"parse_config": [12345, ...], ...}
    """

    def __init__(self) -> None:
        self._samples: dict[str, list[int]] = {}
        self._current: str | None = None
        self._start_ns: int = 0

    def __call__(self, name: str) -> PhaseTimer:
        self._current = name
        return self

    def __enter__(self) -> PhaseTimer:
        self._start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, *a) -> bool:
        elapsed = time.perf_counter_ns() - self._start_ns
        assert self._current is not None
        self._samples.setdefault(self._current, []).append(elapsed)
        self._current = None
        return False

    def results_ns(self) -> dict[str, list[int]]:
        return dict(self._samples)


def _percentile(values: list[float], pct: float) -> float:
    """Return the `pct`th percentile (0..100) by linear interpolation."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def run_fixture(
    fx: Fixture,
    *,
    iterations: int = 5,
    use_api: bool = True,
    skip_cold_start: bool = False,
) -> dict:
    """Run one fixture: warm + N timed + cold-start + token count.

    Returns a per-fixture result dict suitable for the history JSONL.
    """
    # Import here to avoid circular import at module load.
    import pipeline as pl

    cfg_path = str(fx.config_path)

    # Bundled fixtures are trusted by construction; short-circuit the trust
    # gate so the bench doesn't require `bully trust` on every fixture
    # config. Safe because fixtures ship in-repo.
    os.environ["BULLY_TRUST_ALL"] = "1"

    # Warm run (discarded).
    pl.run_pipeline(cfg_path, fx.file_path, fx.diff)

    # Timed runs.
    wall_samples_ns: list[int] = []
    phase_samples_ns: dict[str, list[int]] = {}
    for _ in range(iterations):
        pt = PhaseTimer()
        t0 = time.perf_counter_ns()
        pl.run_pipeline(cfg_path, fx.file_path, fx.diff, phase_timer=pt)
        wall_samples_ns.append(time.perf_counter_ns() - t0)
        for name, samples in pt.results_ns().items():
            # Sum of this phase for this iteration (phases may re-enter).
            phase_samples_ns.setdefault(name, []).append(sum(samples))

    wall_ms = [ns / 1_000_000 for ns in wall_samples_ns]
    phases_ms = {
        name: statistics.median([ns / 1_000_000 for ns in samples])
        for name, samples in phase_samples_ns.items()
    }

    # Cold-start: one subprocess invocation, wall-clock only. Use the
    # default CLI path (not --hook-mode) so the subprocess doesn't block
    # waiting on a Claude Code tool-hook payload.
    cold_start_ms: float | None = None
    if not skip_cold_start:
        pipeline_py = Path(pl.__file__)
        t0 = time.perf_counter_ns()
        subprocess.run(
            [
                sys.executable, str(pipeline_py),
                "--config", cfg_path,
                "--file", fx.file_path,
                "--diff", fx.diff,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        cold_start_ms = (time.perf_counter_ns() - t0) / 1_000_000

    # Tokens: build the real semantic payload and count. Short-circuit
    # when no semantic rules match -- a real run wouldn't dispatch at all.
    rules = pl.parse_config(cfg_path)
    matching = pl.filter_rules(rules, fx.file_path)
    passed = [r.id for r in matching if r.engine in ("script", "ast")]
    semantic = [r for r in matching if r.engine == "semantic"]
    if semantic:
        system = load_evaluator_system_prompt()
        payload = pl.build_semantic_payload(
            fx.file_path, fx.diff, passed, semantic
        )
        tokens, method = count_tokens(
            payload["_evaluator_input"], system=system, use_api=use_api
        )
    else:
        tokens, method = 0, "n/a-no-semantic-rules"

    return {
        "name": fx.name,
        "description": fx.description,
        "wall_ms_p50": statistics.median(wall_ms),
        "wall_ms_p95": _percentile(wall_ms, 95),
        "phases_ms": phases_ms,
        "cold_start_ms": cold_start_ms,
        "tokens": {"input": tokens, "method": method},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bully bench",
        description="Measure bully's speed and input-token cost.",
    )
    parser.add_argument(
        "--config",
        help="Path to a .bully.yml; enables Mode B (config cost analysis).",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Mode A only: diff the last two runs in bench/history.jsonl.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a formatted table.",
    )
    parser.add_argument(
        "--no-tokens",
        action="store_true",
        help="Skip Anthropic API call; use char-count proxy for token counts.",
    )
    parser.add_argument(
        "--fixtures-dir",
        default="bench/fixtures",
        help="Directory of fixture subdirectories (default: bench/fixtures).",
    )
    parser.add_argument(
        "--history",
        default="bench/history.jsonl",
        help="Path to history JSONL (default: bench/history.jsonl).",
    )

    parser.parse_args(argv)

    # Stub: subsequent tasks will dispatch to mode_a / mode_b / compare.
    print("bench: not yet implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
