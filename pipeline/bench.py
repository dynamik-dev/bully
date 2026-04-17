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
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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
    # When bench runs as pipeline.bench (package context), `import pipeline`
    # imports the package (empty __init__). Fall back to the submodule.
    import pipeline as _pl_pkg

    if hasattr(_pl_pkg, "run_pipeline"):
        pl = _pl_pkg
    else:
        import pipeline.pipeline as pl  # type: ignore[no-redef]

    cfg_path = str(fx.config_path)

    # Bundled fixtures are trusted by construction; short-circuit the trust
    # gate so the bench doesn't require `bully trust` on every fixture
    # config. Save-and-restore so this doesn't leak to callers that import
    # the bench as a library.
    prior_trust = os.environ.get("BULLY_TRUST_ALL")
    os.environ["BULLY_TRUST_ALL"] = "1"
    try:
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
                    sys.executable,
                    str(pipeline_py),
                    "--config",
                    cfg_path,
                    "--file",
                    fx.file_path,
                    "--diff",
                    fx.diff,
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
            payload = pl.build_semantic_payload(fx.file_path, fx.diff, passed, semantic)
            tokens, method = count_tokens(payload["_evaluator_input"], system=system, use_api=use_api)
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
    finally:
        if prior_trust is None:
            os.environ.pop("BULLY_TRUST_ALL", None)
        else:
            os.environ["BULLY_TRUST_ALL"] = prior_trust


def _git_sha() -> str | None:
    """Best-effort current git SHA; None if git unavailable or not a repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _git_dirty() -> bool:
    """True iff there are uncommitted changes."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return bool(r.stdout.strip())


def _anthropic_sdk_version() -> str | None:
    mod = _import_anthropic()
    return getattr(mod, "__version__", None) if mod else None


def run_mode_a(
    *,
    fixtures_dir: Path,
    history_path: Path,
    use_api: bool = True,
    iterations: int = 5,
    skip_cold_start: bool = False,
    emit_json: bool = False,
) -> int:
    """Run every fixture in `fixtures_dir`, append a record to `history_path`."""
    fixtures = discover_fixtures(fixtures_dir)
    if not fixtures:
        sys.stderr.write(f"bench: no fixtures found in {fixtures_dir}\n")
        return 1

    results = []
    for fx in fixtures:
        results.append(
            run_fixture(
                fx,
                iterations=iterations,
                use_api=use_api,
                skip_cold_start=skip_cold_start,
            )
        )

    total_wall = sum(r["wall_ms_p50"] for r in results)
    cold_vals = [r["cold_start_ms"] for r in results if r.get("cold_start_ms") is not None]
    total_cold = sum(cold_vals) if cold_vals else None
    total_tokens = sum(r["tokens"]["input"] for r in results)
    methods = {r["tokens"]["method"] for r in results if r["tokens"]["input"]}
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "git_sha": _git_sha(),
        "git_dirty": _git_dirty(),
        "python_version": platform.python_version(),
        "anthropic_sdk_version": _anthropic_sdk_version(),
        "machine": f"{platform.system().lower()}-{platform.release()}",
        "fixtures": results,
        "aggregates": {
            "total_wall_ms_p50": total_wall,
            "total_cold_start_ms": total_cold,
            "total_input_tokens": total_tokens,
            "tokens_method": next(iter(methods)) if len(methods) == 1 else "mixed",
        },
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    if emit_json:
        print(json.dumps(record, indent=2))
    else:
        _print_mode_a_summary(record)
    return 0


def _print_mode_a_summary(record: dict) -> None:
    """Render a human-readable summary of a Mode A record to stdout."""
    print(f"bench run @ {record['ts']}  (sha={record['git_sha'] or '?'})")
    print(f"  python={record['python_version']}  machine={record['machine']}")
    print()
    print(f"  {'fixture':<32} {'wall_p50_ms':>12} {'cold_ms':>10} {'tokens':>9}  method")
    for r in record["fixtures"]:
        cold = r.get("cold_start_ms")
        cold_str = f"{cold:.1f}" if isinstance(cold, (int, float)) else "-"
        print(
            f"  {r['name']:<32} {r['wall_ms_p50']:>12.2f} {cold_str:>10} "
            f"{r['tokens']['input']:>9}  {r['tokens']['method']}"
        )
    agg = record["aggregates"]
    print()
    print(
        f"  totals: wall_p50={agg['total_wall_ms_p50']:.1f}ms  "
        f"cold={agg['total_cold_start_ms'] or 0:.1f}ms  "
        f"tokens={agg['total_input_tokens']} ({agg['tokens_method']})"
    )


def run_compare(*, history_path: Path) -> int:
    """Print deltas between the last two runs in history_path."""
    if not history_path.is_file():
        sys.stderr.write(f"bench: history file not found: {history_path}\n")
        return 1
    lines = [
        json.loads(line)
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) < 2:
        sys.stderr.write("bench: --compare needs at least two runs in history\n")
        return 1

    older, newer = lines[-2], lines[-1]
    print(f"comparing {older.get('git_sha') or '?'} -> {newer.get('git_sha') or '?'}")
    print(f"  {older['ts']}  ->  {newer['ts']}")
    print()

    fx_by_name = {f["name"]: f for f in newer.get("fixtures", [])}
    old_by_name = {f["name"]: f for f in older.get("fixtures", [])}
    all_names = sorted(set(fx_by_name) | set(old_by_name))

    print(f"  {'fixture':<32} {'wall_ms delta':>14} {'tokens delta':>14}")
    for name in all_names:
        new_fx = fx_by_name.get(name, {})
        old_fx = old_by_name.get(name, {})
        dw = new_fx.get("wall_ms_p50", 0) - old_fx.get("wall_ms_p50", 0)
        dt = new_fx.get("tokens", {}).get("input", 0) - old_fx.get("tokens", {}).get("input", 0)
        sign_w = "+" if dw >= 0 else ""
        sign_t = "+" if dt >= 0 else ""
        print(f"  {name:<32} {sign_w}{dw:>13.2f} {sign_t}{dt:>13}")

    agg_new = newer.get("aggregates", {})
    agg_old = older.get("aggregates", {})
    dw_tot = agg_new.get("total_wall_ms_p50", 0) - agg_old.get("total_wall_ms_p50", 0)
    dt_tot = agg_new.get("total_input_tokens", 0) - agg_old.get("total_input_tokens", 0)
    print()
    print(f"  totals: wall_delta={dw_tot:+.2f}ms  tokens_delta={dt_tot:+}")
    return 0


def _synth_diff(added_lines: int, file_path: str = "src/synth.py") -> str:
    """Build a unified diff that adds `added_lines` new lines to a file."""
    body = "".join(f"+line_{i}\n" for i in range(added_lines))
    return f"--- a/{file_path}\n+++ b/{file_path}\n@@ -0,0 +1,{added_lines} @@\n" + body


def run_mode_b(
    *,
    config_path: Path,
    use_api: bool = True,
    emit_json: bool = False,
) -> dict | None:
    """Analyze a .bully.yml's input-token cost.

    Returns the report dict on success, None on failure (error already
    printed to stderr).
    """
    import pipeline as _pl_pkg

    if hasattr(_pl_pkg, "run_pipeline"):
        pl = _pl_pkg
    else:
        import pipeline.pipeline as pl  # type: ignore[no-redef]

    if not config_path.is_file():
        sys.stderr.write(f"bench: config not found: {config_path}\n")
        return None

    try:
        rules = pl.parse_config(str(config_path))
    except pl.ConfigError as e:
        sys.stderr.write(f"bench: config error: {e}\n")
        return None

    semantic_rules = [r for r in rules if r.engine == "semantic"]
    deterministic = [r for r in rules if r.engine in ("script", "ast")]
    system = load_evaluator_system_prompt()

    example_file = "src/example.py"
    floor_payload = pl.build_semantic_payload(example_file, "", [], [])["_evaluator_input"]
    if not semantic_rules:
        floor_tokens, method = 0, "n/a-no-semantic-rules"
    else:
        floor_tokens, method = count_tokens(floor_payload, system=system, use_api=use_api)

    per_rule: list[dict] = []
    for r in semantic_rules:
        payload = pl.build_semantic_payload(example_file, "", [], [r])["_evaluator_input"]
        tokens, _ = count_tokens(payload, system=system, use_api=use_api)
        per_rule.append({"id": r.id, "description": r.description, "tokens": tokens - floor_tokens})
    per_rule.sort(key=lambda x: x["tokens"], reverse=True)

    diff_scaling: list[dict] = []
    for size in (1, 10, 100, 1000):
        payload = pl.build_semantic_payload(
            example_file, _synth_diff(size, example_file), [], semantic_rules
        )["_evaluator_input"]
        tokens, _ = count_tokens(payload, system=system, use_api=use_api)
        diff_scaling.append({"added_lines": size, "total_tokens": tokens})

    scopes: dict[str, int] = {}
    for r in semantic_rules:
        payload = pl.build_semantic_payload(example_file, "", [], [r])["_evaluator_input"]
        tokens, _ = count_tokens(payload, system=system, use_api=use_api)
        for glob in r.scope:
            scopes[glob] = scopes.get(glob, 0) + (tokens - floor_tokens)
    scope_rows = sorted(
        ({"scope": g, "tokens": t} for g, t in scopes.items()),
        key=lambda x: x["tokens"],
        reverse=True,
    )

    report = {
        "config": str(config_path.resolve()),
        "method": method,
        "floor_tokens": floor_tokens,
        "per_rule": per_rule,
        "diff_scaling": diff_scaling,
        "scope_groups": scope_rows,
        "deterministic_rules": [{"id": r.id, "engine": r.engine} for r in deterministic],
    }
    if emit_json:
        print(json.dumps(report, indent=2))
    else:
        _print_mode_b_report(report)
    return report


def _print_mode_b_report(report: dict) -> None:
    print(f"config: {report['config']}")
    print(f"method: {report['method']}")
    print()
    print(f"floor tokens (per dispatch): {report['floor_tokens']}")
    print()
    if report["per_rule"]:
        print(f"  {'rule':<30} {'tokens':>8}")
        for row in report["per_rule"]:
            print(f"  {row['id']:<30} {row['tokens']:>8}")
    else:
        print("  no semantic rules")
    print()
    print("diff scaling (all semantic rules loaded):")
    print(f"  {'added_lines':<14} {'total_tokens':>12}")
    for row in report["diff_scaling"]:
        print(f"  {row['added_lines']:<14} {row['total_tokens']:>12}")
    print()
    if report["deterministic_rules"]:
        print("deterministic rules (0 model tokens):")
        for row in report["deterministic_rules"]:
            print(f"  {row['id']} ({row['engine']})")


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

    args = parser.parse_args(argv)

    if args.config:
        report = run_mode_b(
            config_path=Path(args.config),
            use_api=not args.no_tokens,
            emit_json=args.json,
        )
        return 0 if report is not None else 1
    if args.compare:
        return run_compare(history_path=Path(args.history))
    return run_mode_a(
        fixtures_dir=Path(args.fixtures_dir),
        history_path=Path(args.history),
        use_api=not args.no_tokens,
        emit_json=args.json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
