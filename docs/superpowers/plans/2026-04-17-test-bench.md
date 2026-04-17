# Bully Test Bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a two-mode local bench for measuring bully's speed and input-token cost — a fixture suite that appends to a versioned history log, plus a config-cost analyzer that takes any `.bully.yml` and reports per-invocation token breakdown.

**Architecture:** New `pipeline/bench.py` module holds both modes. Adds a tiny optional `phase_timer` parameter to `run_pipeline` so the bench can time phases without duplicating the pipeline loop. Fixtures live in `bench/fixtures/<name>/` as paired `config.yml` + `fixture.json` files so the production `.bully.yml` parser exercises them end-to-end. Real Anthropic `messages/count_tokens` when available; `len(json.dumps(...))` proxy otherwise. Pipeline stays stdlib-only; `anthropic` is an optional dep.

**Tech Stack:** Python 3.10+, stdlib, `anthropic` (optional), `pytest` + `hypothesis` (already dev deps).

**Reference spec:** `docs/superpowers/specs/2026-04-17-test-bench-design.md`

---

## File Structure

**Create:**
- `pipeline/bench.py` — both modes + helpers (fixture loader, token counter, phase timer, history writer)
- `pipeline/tests/test_bench.py` — unit + integration tests for bench
- `bench/fixtures/<name>/config.yml` — 8 fixture configs
- `bench/fixtures/<name>/fixture.json` — 8 fixture metadata files

**Modify:**
- `pipeline/pipeline.py` — add optional `phase_timer` parameter to `run_pipeline`; add `bench` subcommand dispatch in `main()`
- `pyproject.toml` — add `[project.optional-dependencies] bench = ["anthropic>=0.40"]`
- `README.md` — add a "Bench" section at the end

**Not created (runtime-generated):**
- `bench/history.jsonl` — created by first `bully bench` run; checked into git incrementally per run
- `bench/fixtures/__init__.py` — not needed; bench enumerates the directory

---

## Task 1: Pipeline phase-timer hook (zero-cost by default)

**Files:**
- Modify: `pipeline/pipeline.py` (add import + small edits inside `run_pipeline`)
- Test: `pipeline/tests/test_bench.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `pipeline/tests/test_bench.py`:

```python
"""Bench harness tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline as pl


def test_phase_timer_default_is_noop_and_pipeline_still_runs(tmp_path, monkeypatch):
    """run_pipeline works unchanged when no phase_timer is passed."""
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  - id: trivial\n"
        "    description: trivial\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    target = tmp_path / "x.py"
    target.write_text("x = 1\n")
    result = pl.run_pipeline(str(cfg), str(target), diff="")
    assert result["status"] == "pass"


def test_phase_timer_records_each_phase(tmp_path, monkeypatch):
    """When a phase_timer is passed, it's called for each phase."""
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  - id: trivial\n"
        "    description: trivial\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    target = tmp_path / "x.py"
    target.write_text("x = 1\n")

    seen: list[str] = []

    class Recorder:
        def __call__(self, name):
            seen.append(name)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    pl.run_pipeline(str(cfg), str(target), diff="", phase_timer=Recorder())
    assert "skip_check" in seen
    assert "parse_config" in seen
    assert "filter_rules" in seen
    assert "script_exec" in seen
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest pipeline/tests/test_bench.py -v`
Expected: FAIL — `run_pipeline` has no `phase_timer` keyword argument.

- [ ] **Step 3: Add the phase-timer hook to `run_pipeline`**

In `pipeline/pipeline.py`, near the top of the file (after existing imports), add a no-op timer:

```python
class _NoopPhaseTimer:
    """Default phase timer: every call is a no-op context manager."""

    def __call__(self, name: str) -> "_NoopPhaseTimer":
        return self

    def __enter__(self) -> "_NoopPhaseTimer":
        return self

    def __exit__(self, *a) -> bool:
        return False


_NOOP_PHASE_TIMER = _NoopPhaseTimer()
```

Then modify `run_pipeline`'s signature (around line 1434) to accept the hook:

```python
def run_pipeline(
    config_path: str,
    file_path: str,
    diff: str,
    rule_filter: set[str] | None = None,
    *,
    include_skipped: bool = False,
    phase_timer=_NOOP_PHASE_TIMER,
) -> dict:
```

Wrap the existing phases in `run_pipeline` with `with phase_timer(<name>):` blocks. Phase names to use, in order:

- `skip_check` wraps the `effective_skip_patterns(config_path)` call + `_path_matches_skip` check (the block that currently starts around line 1458)
- `trust_gate` wraps the `_trust_status(config_path)` call (around line 1467)
- `parse_config` wraps the `parse_config(config_path)` call (around line 1483)
- `filter_rules` wraps `filter_rules(rules, file_path)` (around line 1484) and the rule_filter narrow immediately after
- `script_exec` wraps the `for rule in script_rules:` loop (around line 1550)
- `ast_exec` wraps the `if ast_rules:` block (around line 1557)
- `semantic_build` wraps the can't-match filter loop + semantic payload construction (lines 1581 through the end of the semantic section)

The wrapping pattern for each is:

```python
with phase_timer("parse_config"):
    rules = parse_config(config_path)
```

Important: early returns inside a phase (e.g., the skip-gate returning a `"skipped"` dict) work correctly because the context manager's `__exit__` runs on the way out. Don't restructure the early-return logic.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest pipeline/tests/test_bench.py -v`
Expected: PASS, both tests.

- [ ] **Step 5: Run the full existing test suite to catch regressions**

Run: `pytest pipeline/tests -x -q`
Expected: all existing tests pass (the default `_NOOP_PHASE_TIMER` is a no-op).

- [ ] **Step 6: Commit**

```bash
git add pipeline/pipeline.py pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add optional phase_timer hook to run_pipeline

Bench harness needs to measure per-phase timing without duplicating the
pipeline loop. Default is a zero-cost no-op so normal hook invocations
are unaffected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Bench module skeleton + optional `anthropic` dep

**Files:**
- Create: `pipeline/bench.py`
- Modify: `pyproject.toml`
- Modify: `pipeline/pipeline.py` (wire `bully bench` subcommand)
- Test: `pipeline/tests/test_bench.py`

- [ ] **Step 1: Write the failing test**

Append to `pipeline/tests/test_bench.py`:

```python
def test_bench_cli_entrypoint_exists():
    """`bully bench --help` exits cleanly (implies argparse wiring is in place)."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pipeline.pipeline", "bench", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "bench" in (result.stdout + result.stderr).lower()


def test_bench_module_imports():
    """bench module loads without requiring anthropic."""
    from pipeline import bench

    assert hasattr(bench, "main")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest pipeline/tests/test_bench.py::test_bench_module_imports pipeline/tests/test_bench.py::test_bench_cli_entrypoint_exists -v`
Expected: FAIL — `pipeline.bench` doesn't exist; `bully bench` isn't wired.

- [ ] **Step 3: Create `pipeline/bench.py`**

```python
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
import sys
from pathlib import Path


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

    # Stub: subsequent tasks will dispatch to mode_a / mode_b / compare.
    print("bench: not yet implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Wire `bully bench` into the main CLI**

In `pipeline/pipeline.py`, inside `main()`, find the block that checks subcommand flags (starts around line 2222 with `if args.trust:`). Add a check for `bench` **before** the validation-only blocks but after the simple ones. The cleanest location is right after the `if args.hook_mode:` check (around line 2241).

Add:

```python
    if args.bench:
        from pipeline.bench import main as bench_main
        sys.exit(bench_main(args.bench_args))
```

Then wire the argparse side. Find `_parse_args` in the same file (grep for `def _parse_args`). Add a subparser-style flag. Since the existing parser is flat (flags only, no subparsers), do the simplest thing: add a `--bench` passthrough. But `bully bench ...` is already friendlier than `bully --bench ...`, so do this:

At the very top of `main()` (before `_parse_args(sys.argv[1:])`), add a bench short-circuit:

```python
def main() -> None:
    # Short-circuit: `bully bench ...` dispatches to the bench CLI directly,
    # bypassing the main parser (which uses a flat flag model).
    if len(sys.argv) >= 2 and sys.argv[1] == "bench":
        from pipeline.bench import main as bench_main
        sys.exit(bench_main(sys.argv[2:]))

    args = _parse_args(sys.argv[1:])
    ...
```

Do **not** add a `--bench` flag to `_parse_args`. Keep the dispatch surgical.

- [ ] **Step 5: Add optional `bench` dependencies to pyproject.toml**

Modify `pyproject.toml`: in the `[project.optional-dependencies]` section, add a `bench` group. The section should look like:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "ruff>=0.8.0",
    "shellcheck-py>=0.10.0",
    "pre-commit>=3.7",
    "hypothesis>=6.100",
]
bench = ["anthropic>=0.40"]
```

- [ ] **Step 6: Update setuptools packages list**

Still in `pyproject.toml`, the existing `[tool.setuptools]` section has `packages = ["pipeline"]`. No change needed — `pipeline.bench` is already covered.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest pipeline/tests/test_bench.py -v`
Expected: all tests in this task pass.

- [ ] **Step 8: Commit**

```bash
git add pipeline/bench.py pipeline/pipeline.py pyproject.toml pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add bench module skeleton and CLI entry point

`bully bench` now dispatches to pipeline.bench. Flags parsed but not yet
implemented -- subsequent tasks fill in fixture loading, token counting,
and both modes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Fixture loader

**Files:**
- Modify: `pipeline/bench.py`
- Test: `pipeline/tests/test_bench.py`

- [ ] **Step 1: Write the failing test**

Append to `pipeline/tests/test_bench.py`:

```python
def test_load_fixture_reads_config_and_metadata(tmp_path):
    """load_fixture returns a Fixture with config_path + metadata."""
    from pipeline.bench import load_fixture

    fx_dir = tmp_path / "my-fixture"
    fx_dir.mkdir()
    (fx_dir / "config.yml").write_text(
        "rules:\n"
        "  - id: r1\n"
        "    description: d\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    (fx_dir / "fixture.json").write_text(
        '{"name": "my-fixture", "description": "x", '
        '"file_path": "src/a.py", "edit_type": "Edit", "diff": "--- a\\n"}'
    )
    fx = load_fixture(fx_dir)
    assert fx.name == "my-fixture"
    assert fx.file_path == "src/a.py"
    assert fx.edit_type == "Edit"
    assert fx.diff.startswith("--- a")
    assert fx.config_path.name == "config.yml"
    assert fx.config_path.exists()


def test_load_fixture_rejects_missing_files(tmp_path):
    """load_fixture raises when either expected file is missing."""
    from pipeline.bench import FixtureError, load_fixture

    fx_dir = tmp_path / "bad"
    fx_dir.mkdir()
    # Missing both files.
    import pytest
    with pytest.raises(FixtureError, match="config.yml"):
        load_fixture(fx_dir)


def test_load_fixture_rejects_malformed_json(tmp_path):
    """load_fixture raises a clear error on malformed metadata JSON."""
    from pipeline.bench import FixtureError, load_fixture

    fx_dir = tmp_path / "bad-json"
    fx_dir.mkdir()
    (fx_dir / "config.yml").write_text("rules: []\n")
    (fx_dir / "fixture.json").write_text("{not json")
    import pytest
    with pytest.raises(FixtureError, match="fixture.json"):
        load_fixture(fx_dir)


def test_discover_fixtures_lists_all_subdirs(tmp_path):
    """discover_fixtures returns a sorted list of fixture directories."""
    from pipeline.bench import discover_fixtures

    for name in ["zeta", "alpha", "mu"]:
        d = tmp_path / name
        d.mkdir()
        (d / "config.yml").write_text("rules: []\n")
        (d / "fixture.json").write_text(
            '{"name": "' + name + '", "description": "", '
            '"file_path": "a.py", "edit_type": "Edit", "diff": ""}'
        )
    result = discover_fixtures(tmp_path)
    assert [f.name for f in result] == ["alpha", "mu", "zeta"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest pipeline/tests/test_bench.py::test_load_fixture_reads_config_and_metadata -v`
Expected: FAIL — `load_fixture` not defined.

- [ ] **Step 3: Add the fixture loader to `pipeline/bench.py`**

Add to `pipeline/bench.py`, above `main()`:

```python
import json
from dataclasses import dataclass


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest pipeline/tests/test_bench.py -v`
Expected: all four new tests pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bench.py pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add bench fixture loader

Fixtures live in bench/fixtures/<name>/{config.yml,fixture.json}.
load_fixture validates both files exist and the metadata has required
fields; discover_fixtures returns all subdirectories sorted by name.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Token-counting helper with proxy fallback

**Files:**
- Modify: `pipeline/bench.py`
- Test: `pipeline/tests/test_bench.py`

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_bench.py`:

```python
def test_count_tokens_proxy_when_no_api_key(monkeypatch):
    """count_tokens falls back to char-count when no API key is present."""
    from pipeline.bench import count_tokens

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = {"file": "x.py", "diff": "hello", "evaluate": []}
    count, method = count_tokens(payload, system="sys prompt")
    assert method == "proxy"
    assert count == len(
        __import__("json").dumps(payload)
    ) + len("sys prompt")


def test_count_tokens_proxy_when_anthropic_missing(monkeypatch):
    """If `anthropic` is not importable, fall back to proxy even with key set."""
    from pipeline import bench

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bench, "_import_anthropic", lambda: None)

    payload = {"x": 1}
    count, method = bench.count_tokens(payload, system="s")
    assert method == "proxy"


def test_count_tokens_api_path(monkeypatch):
    """With API key + anthropic client, call messages.count_tokens."""
    from pipeline import bench

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class FakeResp:
        input_tokens = 42

    class FakeMessages:
        def count_tokens(self, **kwargs):
            assert kwargs["model"] == bench.BENCH_MODEL
            assert kwargs["system"] == "s"
            assert "content" in kwargs["messages"][0]
            return FakeResp()

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    class FakeAnthropic:
        Anthropic = FakeClient

    monkeypatch.setattr(bench, "_import_anthropic", lambda: FakeAnthropic)

    count, method = bench.count_tokens({"a": 1}, system="s")
    assert count == 42
    assert method == "count_tokens"


def test_load_evaluator_system_prompt_strips_frontmatter(tmp_path, monkeypatch):
    """System prompt is read from agents/bully-evaluator.md without frontmatter."""
    from pipeline import bench

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "bully-evaluator.md").write_text(
        "---\n"
        "name: bully-evaluator\n"
        "model: sonnet\n"
        "---\n"
        "\n"
        "You are the evaluator. Apply each rule.\n"
    )
    monkeypatch.setattr(bench, "_repo_root", lambda: tmp_path)

    text = bench.load_evaluator_system_prompt()
    assert text.startswith("You are the evaluator")
    assert "---" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest pipeline/tests/test_bench.py -v -k "count_tokens or evaluator_system"`
Expected: FAIL — `count_tokens`, `_import_anthropic`, `BENCH_MODEL`, `load_evaluator_system_prompt` not defined.

- [ ] **Step 3: Add token-counting and prompt-loading helpers**

Add to `pipeline/bench.py`, above `main()`:

```python
import os


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest pipeline/tests/test_bench.py -v -k "count_tokens or evaluator_system"`
Expected: all four tests pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/bench.py pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add token counter with Anthropic SDK + proxy fallback

count_tokens uses messages/count_tokens when an API key and the SDK are
available; otherwise returns len(json.dumps(payload)) + len(system) and
tags the result 'proxy'. System prompt is loaded from the real
bully-evaluator agent file so counts match production.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Phase-timer implementation + single-fixture runner

**Files:**
- Modify: `pipeline/bench.py`
- Test: `pipeline/tests/test_bench.py`

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_bench.py`:

```python
def test_phasetimer_records_all_phase_durations():
    """PhaseTimer records a list of (name, ns) for each phase invocation."""
    from pipeline.bench import PhaseTimer
    import time

    pt = PhaseTimer()
    with pt("a"):
        time.sleep(0.001)
    with pt("b"):
        time.sleep(0.002)
    results = pt.results_ns()
    assert "a" in results
    assert "b" in results
    # Each phase tracked at least one sample.
    assert len(results["a"]) == 1
    assert len(results["b"]) == 1
    assert results["a"][0] > 0
    assert results["b"][0] > results["a"][0] - 500_000  # loose ordering sanity


def test_run_fixture_returns_structured_result(tmp_path, monkeypatch):
    """run_fixture returns per-phase median/p95 plus cold-start and tokens."""
    from pipeline.bench import Fixture, run_fixture

    fx_dir = tmp_path / "fx"
    fx_dir.mkdir()
    cfg = fx_dir / "config.yml"
    cfg.write_text(
        "rules:\n"
        "  - id: r1\n"
        "    description: d\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    (fx_dir / "fixture.json").write_text(
        '{"name": "fx", "description": "x", "file_path": "a.py",'
        ' "edit_type": "Edit", "diff": ""}'
    )
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")

    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    monkeypatch.chdir(tmp_path)

    fx = Fixture(
        name="fx",
        description="x",
        file_path=str(target),
        edit_type="Edit",
        diff="",
        config_path=cfg,
    )
    result = run_fixture(fx, iterations=3, use_api=False, skip_cold_start=True)
    assert result["name"] == "fx"
    assert "wall_ms_p50" in result
    assert "wall_ms_p95" in result
    assert "phases_ms" in result
    assert "skip_check" in result["phases_ms"]
    assert "parse_config" in result["phases_ms"]
    assert result["tokens"]["method"] == "proxy"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest pipeline/tests/test_bench.py -v -k "phasetimer or run_fixture"`
Expected: FAIL — `PhaseTimer` and `run_fixture` not defined.

- [ ] **Step 3: Add `PhaseTimer` and `run_fixture` to `pipeline/bench.py`**

Add to `pipeline/bench.py`:

```python
import statistics
import subprocess
import time


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

    def __call__(self, name: str) -> "PhaseTimer":
        self._current = name
        return self

    def __enter__(self) -> "PhaseTimer":
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
    fx: "Fixture",
    *,
    iterations: int = 5,
    use_api: bool = True,
    skip_cold_start: bool = False,
) -> dict:
    """Run one fixture: warm + N timed + cold-start + token count.

    Returns a per-fixture result dict suitable for the history JSONL.
    """
    # Import here to avoid a circular import at module load.
    from pipeline import pipeline as pl

    cfg_path = str(fx.config_path)

    # Bundled fixtures are trusted by construction; short-circuit the
    # trust gate so the bench doesn't require `bully trust` on every
    # fixture config. Safe because fixtures ship in-repo.
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

    # Tokens: build the real semantic payload and count.
    rules = pl.parse_config(cfg_path)
    matching = pl.filter_rules(rules, fx.file_path)
    passed = [r.id for r in matching if r.engine in ("script", "ast")]
    semantic = [r for r in matching if r.engine == "semantic"]
    if semantic:
        payload = pl.build_semantic_payload(
            fx.file_path, fx.diff, passed, semantic
        )
        system = load_evaluator_system_prompt()
        tokens, method = count_tokens(payload["_evaluator_input"],
                                      system=system, use_api=use_api)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest pipeline/tests/test_bench.py -v -k "phasetimer or run_fixture"`
Expected: both tests pass.

- [ ] **Step 5: Run the full bench test file to catch any regressions**

Run: `pytest pipeline/tests/test_bench.py -v`
Expected: all bench tests pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/bench.py pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add PhaseTimer and single-fixture runner

PhaseTimer implements the pipeline hook protocol and records per-phase
elapsed time. run_fixture runs one fixture N times in-process, samples
cold-start once via subprocess, and builds the real semantic payload to
count tokens.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Mode A — run all fixtures + write history

**Files:**
- Modify: `pipeline/bench.py`
- Test: `pipeline/tests/test_bench.py`

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_bench.py`:

```python
def test_run_mode_a_writes_history_line(tmp_path, monkeypatch):
    """Mode A writes one JSONL line per run with fixture results + aggregates."""
    from pipeline.bench import run_mode_a

    # Build two fixtures.
    fixtures_root = tmp_path / "fixtures"
    for name in ("a", "b"):
        d = fixtures_root / name
        d.mkdir(parents=True)
        (d / "config.yml").write_text(
            "rules:\n"
            "  - id: r\n"
            "    description: d\n"
            "    engine: script\n"
            "    scope: '**/*.py'\n"
            "    severity: warning\n"
            "    script: 'exit 0'\n"
        )
        (d / "fixture.json").write_text(
            '{"name": "' + name + '", "description": "x",'
            ' "file_path": "x.py", "edit_type": "Edit", "diff": ""}'
        )
    (tmp_path / "x.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")

    history_path = tmp_path / "history.jsonl"
    rc = run_mode_a(
        fixtures_dir=fixtures_root,
        history_path=history_path,
        use_api=False,
        iterations=2,
        skip_cold_start=True,
        emit_json=True,
    )
    assert rc == 0
    assert history_path.is_file()
    lines = history_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "ts" in record
    assert "fixtures" in record
    assert len(record["fixtures"]) == 2
    assert {f["name"] for f in record["fixtures"]} == {"a", "b"}
    assert "aggregates" in record
    assert "total_wall_ms_p50" in record["aggregates"]


def test_run_mode_a_errors_when_no_fixtures(tmp_path, capsys):
    """Mode A returns non-zero and prints an error when no fixtures exist."""
    from pipeline.bench import run_mode_a

    history_path = tmp_path / "h.jsonl"
    rc = run_mode_a(
        fixtures_dir=tmp_path / "missing",
        history_path=history_path,
        use_api=False,
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "no fixtures" in err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest pipeline/tests/test_bench.py -v -k "mode_a"`
Expected: FAIL — `run_mode_a` not defined.

- [ ] **Step 3: Add `run_mode_a` and helpers**

Add to `pipeline/bench.py`:

```python
import platform
from datetime import datetime, timezone


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
    total_cold = sum(
        r["cold_start_ms"] for r in results if r.get("cold_start_ms") is not None
    ) or None
    total_tokens = sum(r["tokens"]["input"] for r in results)
    methods = {r["tokens"]["method"] for r in results if r["tokens"]["input"]}
    record = {
        "ts": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
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
    print(f"  {'fixture':<32} {'wall_p50_ms':>12} {'cold_ms':>10} "
          f"{'tokens':>9}  method")
    for r in record["fixtures"]:
        cold = r.get("cold_start_ms")
        cold_str = f"{cold:.1f}" if isinstance(cold, (int, float)) else "-"
        print(
            f"  {r['name']:<32} {r['wall_ms_p50']:>12.2f} {cold_str:>10} "
            f"{r['tokens']['input']:>9}  {r['tokens']['method']}"
        )
    agg = record["aggregates"]
    print()
    print(f"  totals: wall_p50={agg['total_wall_ms_p50']:.1f}ms  "
          f"cold={agg['total_cold_start_ms'] or 0:.1f}ms  "
          f"tokens={agg['total_input_tokens']} ({agg['tokens_method']})")
```

- [ ] **Step 4: Wire Mode A into `main()` (default path when `--config` is absent)**

Replace the stub at the bottom of `main()` in `pipeline/bench.py`:

```python
    # Dispatch.
    if args.config:
        sys.stderr.write("bench: mode B (--config) not yet implemented\n")
        return 1
    if args.compare:
        sys.stderr.write("bench: --compare not yet implemented\n")
        return 1
    return run_mode_a(
        fixtures_dir=Path(args.fixtures_dir),
        history_path=Path(args.history),
        use_api=not args.no_tokens,
        emit_json=args.json,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest pipeline/tests/test_bench.py -v -k "mode_a"`
Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/bench.py pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add Mode A: fixture suite runner + history writer

`bully bench` now enumerates bench/fixtures/*, runs each fixture, and
appends one JSONL line to bench/history.jsonl with per-fixture phase
timings + aggregate totals. Human-readable summary to stdout by
default; --json emits the raw record.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Mode A --compare

**Files:**
- Modify: `pipeline/bench.py`
- Test: `pipeline/tests/test_bench.py`

- [ ] **Step 1: Write the failing test**

Append to `pipeline/tests/test_bench.py`:

```python
def test_compare_reports_deltas_between_last_two_runs(tmp_path, capsys):
    """--compare prints a delta table for the last two history entries."""
    from pipeline.bench import run_compare

    history = tmp_path / "h.jsonl"
    older = {
        "ts": "2026-04-15T10:00:00Z",
        "git_sha": "aaa",
        "fixtures": [
            {"name": "a", "wall_ms_p50": 10.0, "tokens": {"input": 100,
                "method": "count_tokens"}},
        ],
        "aggregates": {
            "total_wall_ms_p50": 10.0,
            "total_input_tokens": 100,
        },
    }
    newer = {
        "ts": "2026-04-17T12:00:00Z",
        "git_sha": "bbb",
        "fixtures": [
            {"name": "a", "wall_ms_p50": 15.0, "tokens": {"input": 120,
                "method": "count_tokens"}},
        ],
        "aggregates": {
            "total_wall_ms_p50": 15.0,
            "total_input_tokens": 120,
        },
    }
    history.write_text(json.dumps(older) + "\n" + json.dumps(newer) + "\n")

    rc = run_compare(history_path=history)
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaa" in out and "bbb" in out
    assert "+5.00" in out or "+50.0" in out  # wall delta
    assert "+20" in out  # token delta


def test_compare_fails_when_fewer_than_two_runs(tmp_path, capsys):
    """--compare needs at least two runs to produce a delta."""
    from pipeline.bench import run_compare

    history = tmp_path / "h.jsonl"
    history.write_text(json.dumps({"ts": "t", "fixtures": [], "aggregates": {}}) + "\n")

    rc = run_compare(history_path=history)
    assert rc != 0
    err = capsys.readouterr().err
    assert "two runs" in err.lower() or "2 runs" in err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest pipeline/tests/test_bench.py -v -k "compare"`
Expected: FAIL — `run_compare` not defined.

- [ ] **Step 3: Add `run_compare`**

Add to `pipeline/bench.py`:

```python
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
        sys.stderr.write(
            "bench: --compare needs at least two runs in history\n"
        )
        return 1

    older, newer = lines[-2], lines[-1]
    print(f"comparing {older.get('git_sha') or '?'} -> "
          f"{newer.get('git_sha') or '?'}")
    print(f"  {older['ts']}  ->  {newer['ts']}")
    print()

    fx_by_name = {f["name"]: f for f in newer.get("fixtures", [])}
    old_by_name = {f["name"]: f for f in older.get("fixtures", [])}
    all_names = sorted(set(fx_by_name) | set(old_by_name))

    print(f"  {'fixture':<32} {'Δ wall_ms':>12} {'Δ tokens':>12}")
    for name in all_names:
        new_fx = fx_by_name.get(name, {})
        old_fx = old_by_name.get(name, {})
        dw = new_fx.get("wall_ms_p50", 0) - old_fx.get("wall_ms_p50", 0)
        dt = new_fx.get("tokens", {}).get("input", 0) - old_fx.get(
            "tokens", {}
        ).get("input", 0)
        sign_w = "+" if dw >= 0 else ""
        sign_t = "+" if dt >= 0 else ""
        print(f"  {name:<32} {sign_w}{dw:>11.2f} {sign_t}{dt:>11}")

    agg_new = newer.get("aggregates", {})
    agg_old = older.get("aggregates", {})
    dw_tot = agg_new.get("total_wall_ms_p50", 0) - agg_old.get(
        "total_wall_ms_p50", 0
    )
    dt_tot = agg_new.get("total_input_tokens", 0) - agg_old.get(
        "total_input_tokens", 0
    )
    print()
    print(f"  totals: Δwall={dw_tot:+.2f}ms  Δtokens={dt_tot:+}")
    return 0
```

- [ ] **Step 4: Wire `--compare` into `main()`**

In `pipeline/bench.py`, replace the compare stub in `main()`:

```python
    if args.compare:
        return run_compare(history_path=Path(args.history))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest pipeline/tests/test_bench.py -v -k "compare"`
Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/bench.py pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add bench --compare: diff last two runs in history

Per-fixture + aggregate deltas for wall time and input tokens. Fails
clearly when history has fewer than two runs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Mode B — config cost analysis

**Files:**
- Modify: `pipeline/bench.py`
- Test: `pipeline/tests/test_bench.py`

- [ ] **Step 1: Write the failing tests**

Append to `pipeline/tests/test_bench.py`:

```python
def test_mode_b_reports_floor_and_per_rule(tmp_path, monkeypatch):
    """Mode B computes floor, per-rule marginal, and diff scaling."""
    from pipeline.bench import run_mode_b

    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  - id: sem-long\n"
        "    description: 'A somewhat long description that should cost more tokens than a short one'\n"
        "    engine: semantic\n"
        "    scope: '**/*.py'\n"
        "    severity: error\n"
        "  - id: sem-short\n"
        "    description: short\n"
        "    engine: semantic\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "  - id: script-only\n"
        "    description: scripted check\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: error\n"
        "    script: 'exit 0'\n"
    )
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")

    result = run_mode_b(config_path=cfg, use_api=False, emit_json=True)
    assert result["returncode"] == 0
    report = result["report"]
    assert report["floor_tokens"] > 0
    assert len(report["per_rule"]) == 2  # two semantic rules
    long_cost = next(
        r["tokens"] for r in report["per_rule"] if r["id"] == "sem-long"
    )
    short_cost = next(
        r["tokens"] for r in report["per_rule"] if r["id"] == "sem-short"
    )
    assert long_cost > short_cost
    assert "diff_scaling" in report
    sizes = [row["added_lines"] for row in report["diff_scaling"]]
    assert sizes == [1, 10, 100, 1000]
    # Monotonically non-decreasing as diff grows.
    totals = [row["total_tokens"] for row in report["diff_scaling"]]
    assert totals == sorted(totals)
    # Script rule listed separately as zero-cost model-wise.
    assert any(r["id"] == "script-only" for r in report["deterministic_rules"])


def test_mode_b_handles_config_with_no_semantic_rules(tmp_path, monkeypatch):
    """Empty-semantic config reports floor=0 and empty per-rule."""
    from pipeline.bench import run_mode_b

    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  - id: scripted\n"
        "    description: d\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")

    result = run_mode_b(config_path=cfg, use_api=False, emit_json=True)
    assert result["returncode"] == 0
    report = result["report"]
    assert report["floor_tokens"] == 0
    assert report["per_rule"] == []
    assert len(report["deterministic_rules"]) == 1


def test_mode_b_errors_when_config_missing(tmp_path, capsys):
    """Missing config path yields a clear error."""
    from pipeline.bench import run_mode_b

    result = run_mode_b(config_path=tmp_path / "nope.yml", use_api=False)
    assert result["returncode"] != 0
    err = capsys.readouterr().err
    assert "not found" in err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest pipeline/tests/test_bench.py -v -k "mode_b"`
Expected: FAIL — `run_mode_b` not defined.

- [ ] **Step 3: Add `run_mode_b`**

Add to `pipeline/bench.py`:

```python
def _synth_diff(added_lines: int, file_path: str = "src/synth.py") -> str:
    """Build a unified diff that adds `added_lines` new lines to a file."""
    body = "".join(f"+line_{i}\n" for i in range(added_lines))
    return (
        f"--- a/{file_path}\n"
        f"+++ b/{file_path}\n"
        f"@@ -0,0 +1,{added_lines} @@\n"
        + body
    )


def run_mode_b(
    *,
    config_path: Path,
    use_api: bool = True,
    emit_json: bool = False,
) -> dict:
    """Analyze a .bully.yml's input-token cost.

    Returns {"returncode": int, "report": {...} | None}.
    """
    from pipeline import pipeline as pl

    if not config_path.is_file():
        sys.stderr.write(f"bench: config not found: {config_path}\n")
        return {"returncode": 1, "report": None}

    try:
        rules = pl.parse_config(str(config_path))
    except pl.ConfigError as e:
        sys.stderr.write(f"bench: config error: {e}\n")
        return {"returncode": 1, "report": None}

    semantic_rules = [r for r in rules if r.engine == "semantic"]
    deterministic = [r for r in rules if r.engine in ("script", "ast")]
    system = load_evaluator_system_prompt()

    example_file = "src/example.py"
    floor_payload = pl.build_semantic_payload(
        example_file, "", [], []
    )["_evaluator_input"]
    if not semantic_rules:
        floor_tokens, method = 0, "n/a-no-semantic-rules"
    else:
        floor_tokens, method = count_tokens(
            floor_payload, system=system, use_api=use_api
        )

    per_rule: list[dict] = []
    for r in semantic_rules:
        payload = pl.build_semantic_payload(
            example_file, "", [], [r]
        )["_evaluator_input"]
        tokens, _ = count_tokens(payload, system=system, use_api=use_api)
        per_rule.append(
            {"id": r.id, "description": r.description,
             "tokens": tokens - floor_tokens}
        )
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
        payload = pl.build_semantic_payload(
            example_file, "", [], [r]
        )["_evaluator_input"]
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
        "deterministic_rules": [
            {"id": r.id, "engine": r.engine} for r in deterministic
        ],
    }
    if emit_json:
        print(json.dumps(report, indent=2))
    else:
        _print_mode_b_report(report)
    return {"returncode": 0, "report": report}


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
```

- [ ] **Step 4: Wire Mode B into `main()`**

Replace the Mode B stub in `main()`:

```python
    if args.config:
        result = run_mode_b(
            config_path=Path(args.config),
            use_api=not args.no_tokens,
            emit_json=args.json,
        )
        return result["returncode"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest pipeline/tests/test_bench.py -v -k "mode_b"`
Expected: all three tests pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/bench.py pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add Mode B: config cost analysis

`bully bench --config <path>` parses a .bully.yml and reports floor
tokens per dispatch, per-rule marginal cost (sorted), diff scaling at
1/10/100/1000 added lines, and a scope-grouped breakdown. Script and
ast rules are listed separately as zero model-token cost.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Author the 8 bench fixtures

**Files:**
- Create: `bench/fixtures/01-script-only-small-diff/config.yml`
- Create: `bench/fixtures/01-script-only-small-diff/fixture.json`
- Create: `bench/fixtures/02-ast-only-small-diff/config.yml`
- Create: `bench/fixtures/02-ast-only-small-diff/fixture.json`
- Create: `bench/fixtures/03-semantic-only-small-diff/config.yml`
- Create: `bench/fixtures/03-semantic-only-small-diff/fixture.json`
- Create: `bench/fixtures/04-mixed-engines/config.yml`
- Create: `bench/fixtures/04-mixed-engines/fixture.json`
- Create: `bench/fixtures/05-big-extends-chain/{parent-a.yml,parent-b.yml,config.yml,fixture.json}`
- Create: `bench/fixtures/06-many-semantic-rules/config.yml`
- Create: `bench/fixtures/06-many-semantic-rules/fixture.json`
- Create: `bench/fixtures/07-large-diff/config.yml`
- Create: `bench/fixtures/07-large-diff/fixture.json`
- Create: `bench/fixtures/08-auto-generated-skip/config.yml`
- Create: `bench/fixtures/08-auto-generated-skip/fixture.json`

- [ ] **Step 1: Create fixture 01 — script-only-small-diff**

`bench/fixtures/01-script-only-small-diff/config.yml`:

```yaml
rules:
  - id: no-print
    description: Disallow bare print() in production Python code
    engine: script
    scope: "**/*.py"
    severity: warning
    script: "grep -n 'print(' {file} && exit 1 || exit 0"
```

`bench/fixtures/01-script-only-small-diff/fixture.json`:

```json
{
  "name": "01-script-only-small-diff",
  "description": "One script rule, small Python edit",
  "file_path": "bench/fixtures/01-script-only-small-diff/target.py",
  "edit_type": "Edit",
  "diff": "--- a/target.py\n+++ b/target.py\n@@ -1,3 +1,4 @@\n def main():\n+    x = 1\n     return 0\n"
}
```

Also create `bench/fixtures/01-script-only-small-diff/target.py`:

```python
def main():
    x = 1
    return 0
```

- [ ] **Step 2: Create fixture 02 — ast-only-small-diff**

`bench/fixtures/02-ast-only-small-diff/config.yml`:

```yaml
rules:
  - id: no-var
    description: Prefer let/const over var in TypeScript
    engine: ast
    scope: "**/*.ts"
    severity: warning
    language: ts
    pattern: "var $X = $Y"
```

`bench/fixtures/02-ast-only-small-diff/fixture.json`:

```json
{
  "name": "02-ast-only-small-diff",
  "description": "One ast rule, small TypeScript edit",
  "file_path": "bench/fixtures/02-ast-only-small-diff/target.ts",
  "edit_type": "Edit",
  "diff": "--- a/target.ts\n+++ b/target.ts\n@@ -1,2 +1,3 @@\n function main() {\n+  const x = 1;\n }\n"
}
```

`bench/fixtures/02-ast-only-small-diff/target.ts`:

```ts
function main() {
  const x = 1;
}
```

- [ ] **Step 3: Create fixture 03 — semantic-only-small-diff**

`bench/fixtures/03-semantic-only-small-diff/config.yml`:

```yaml
rules:
  - id: no-hardcoded-secrets
    description: >
      Do not hardcode API keys, tokens, passwords, or other secret credentials
      in source code. Values that look like credentials should be loaded from
      environment variables or a secret manager.
    engine: semantic
    scope: "**/*.py"
    severity: error
```

`bench/fixtures/03-semantic-only-small-diff/fixture.json`:

```json
{
  "name": "03-semantic-only-small-diff",
  "description": "One semantic rule, small Python edit (no violation)",
  "file_path": "bench/fixtures/03-semantic-only-small-diff/target.py",
  "edit_type": "Edit",
  "diff": "--- a/target.py\n+++ b/target.py\n@@ -1,2 +1,3 @@\n def main():\n+    api_url = 'https://example.com'\n     pass\n"
}
```

`bench/fixtures/03-semantic-only-small-diff/target.py`:

```python
def main():
    api_url = 'https://example.com'
    pass
```

- [ ] **Step 4: Create fixture 04 — mixed-engines**

`bench/fixtures/04-mixed-engines/config.yml`:

```yaml
rules:
  - id: no-print
    description: Disallow bare print() in production Python code
    engine: script
    scope: "**/*.py"
    severity: warning
    script: "grep -n 'print(' {file} && exit 1 || exit 0"
  - id: no-eval
    description: Disallow eval() usage
    engine: ast
    scope: "**/*.py"
    severity: error
    language: python
    pattern: "eval($X)"
  - id: no-hardcoded-secrets
    description: Do not hardcode API keys, tokens, or passwords.
    engine: semantic
    scope: "**/*.py"
    severity: error
```

`bench/fixtures/04-mixed-engines/fixture.json`:

```json
{
  "name": "04-mixed-engines",
  "description": "One rule per engine, moderate Python edit",
  "file_path": "bench/fixtures/04-mixed-engines/target.py",
  "edit_type": "Edit",
  "diff": "--- a/target.py\n+++ b/target.py\n@@ -1,4 +1,8 @@\n import os\n \n def main():\n+    x = os.getenv('X')\n+    y = {'a': 1, 'b': 2}\n+    for k, v in y.items():\n+        pass\n     return 0\n"
}
```

`bench/fixtures/04-mixed-engines/target.py`:

```python
import os

def main():
    x = os.getenv('X')
    y = {'a': 1, 'b': 2}
    for k, v in y.items():
        pass
    return 0
```

- [ ] **Step 5: Create fixture 05 — big-extends-chain**

`bench/fixtures/05-big-extends-chain/grandparent.yml`:

```yaml
rules:
  - id: grandparent-rule
    description: A rule defined three levels up the extends chain
    engine: script
    scope: "**/*.py"
    severity: warning
    script: "exit 0"
```

`bench/fixtures/05-big-extends-chain/parent.yml`:

```yaml
extends: grandparent.yml
rules:
  - id: parent-rule
    description: A rule defined two levels up
    engine: script
    scope: "**/*.py"
    severity: warning
    script: "exit 0"
```

`bench/fixtures/05-big-extends-chain/config.yml`:

```yaml
extends: parent.yml
rules:
  - id: local-rule
    description: A rule defined at the leaf
    engine: script
    scope: "**/*.py"
    severity: warning
    script: "exit 0"
```

`bench/fixtures/05-big-extends-chain/fixture.json`:

```json
{
  "name": "05-big-extends-chain",
  "description": "Three-level extends chain; stresses parser + skip walker",
  "file_path": "bench/fixtures/05-big-extends-chain/target.py",
  "edit_type": "Edit",
  "diff": "--- a/target.py\n+++ b/target.py\n@@ -1 +1,2 @@\n+x = 1\n y = 2\n"
}
```

`bench/fixtures/05-big-extends-chain/target.py`:

```python
x = 1
y = 2
```

- [ ] **Step 6: Create fixture 06 — many-semantic-rules**

`bench/fixtures/06-many-semantic-rules/config.yml` — 20 semantic rules with varied descriptions:

```yaml
rules:
  - id: sem-01
    description: Do not hardcode API keys, tokens, passwords, or other secret credentials in source code.
    engine: semantic
    scope: "**/*.py"
    severity: error
  - id: sem-02
    description: Avoid broad exception handlers that swallow errors silently.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-03
    description: Avoid mutable default arguments in function signatures.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-04
    description: Use context managers for file and resource handling.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-05
    description: Do not use assert for input validation in production code paths.
    engine: semantic
    scope: "**/*.py"
    severity: error
  - id: sem-06
    description: Avoid globally-mutable state; prefer explicit dependency injection.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-07
    description: Do not catch Exception or BaseException without re-raising.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-08
    description: Prefer f-strings over .format() and %-formatting.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-09
    description: Do not log sensitive values (passwords, tokens, PII).
    engine: semantic
    scope: "**/*.py"
    severity: error
  - id: sem-10
    description: Avoid running subprocess with shell=True unless the command is shlex-quoted.
    engine: semantic
    scope: "**/*.py"
    severity: error
  - id: sem-11
    description: Prefer pathlib.Path over os.path for new code.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-12
    description: Do not use print() for operational logging; use the logging module.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-13
    description: Avoid time.sleep() in request handlers.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-14
    description: Prefer list/dict/set comprehensions over map+filter when it improves clarity.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-15
    description: Do not silently drop exceptions in background tasks.
    engine: semantic
    scope: "**/*.py"
    severity: error
  - id: sem-16
    description: Check for file existence with Path.exists() rather than try/except FileNotFoundError when not needed.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-17
    description: Do not commit debugging breakpoint()/pdb.set_trace() calls.
    engine: semantic
    scope: "**/*.py"
    severity: error
  - id: sem-18
    description: Prefer dataclasses over manually-defined __init__/__eq__/__repr__ for data-holding classes.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-19
    description: Use type hints on public function signatures.
    engine: semantic
    scope: "**/*.py"
    severity: warning
  - id: sem-20
    description: Do not hard-code environment-specific paths; use configuration.
    engine: semantic
    scope: "**/*.py"
    severity: warning
```

`bench/fixtures/06-many-semantic-rules/fixture.json`:

```json
{
  "name": "06-many-semantic-rules",
  "description": "20 semantic rules, small Python edit; stresses payload size",
  "file_path": "bench/fixtures/06-many-semantic-rules/target.py",
  "edit_type": "Edit",
  "diff": "--- a/target.py\n+++ b/target.py\n@@ -1,2 +1,3 @@\n import os\n+path = os.path.join('/tmp', 'f')\n x = 1\n"
}
```

`bench/fixtures/06-many-semantic-rules/target.py`:

```python
import os
path = os.path.join('/tmp', 'f')
x = 1
```

- [ ] **Step 7: Create fixture 07 — large-diff**

`bench/fixtures/07-large-diff/config.yml`:

```yaml
rules:
  - id: no-hardcoded-secrets
    description: Do not hardcode API keys, tokens, or passwords.
    engine: semantic
    scope: "**/*.py"
    severity: error
```

For the large diff, generate a 500-line diff via Python one-liner in the fixture itself. Because fixture.json must hold the diff inline, use a script to generate it:

```bash
python3 -c "
import json
added = ''.join(f'+    line_{i} = {i}\n' for i in range(500))
header = '--- a/bench/fixtures/07-large-diff/target.py\n+++ b/bench/fixtures/07-large-diff/target.py\n@@ -1 +1,501 @@\n def main():\n'
print(json.dumps({
    'name': '07-large-diff',
    'description': '500-line diff, one semantic rule; scales semantic payload',
    'file_path': 'bench/fixtures/07-large-diff/target.py',
    'edit_type': 'Edit',
    'diff': header + added,
}, indent=2))
" > bench/fixtures/07-large-diff/fixture.json
```

And the target file:

`bench/fixtures/07-large-diff/target.py`:

```python
def main():
```

- [ ] **Step 8: Create fixture 08 — auto-generated-skip**

`bench/fixtures/08-auto-generated-skip/config.yml`:

```yaml
rules:
  - id: noop
    description: A rule that should never be evaluated because skip shortcircuits first
    engine: script
    scope: "**/*.js"
    severity: warning
    script: "exit 0"
```

`bench/fixtures/08-auto-generated-skip/fixture.json`:

```json
{
  "name": "08-auto-generated-skip",
  "description": "File matches SKIP_PATTERNS (*.min.js); pipeline should short-circuit",
  "file_path": "bench/fixtures/08-auto-generated-skip/bundle.min.js",
  "edit_type": "Edit",
  "diff": "--- a/bundle.min.js\n+++ b/bundle.min.js\n@@ -1 +1 @@\n-var a=1\n+var a=2\n"
}
```

`bench/fixtures/08-auto-generated-skip/bundle.min.js`:

```
var a=2
```

- [ ] **Step 9: Run end-to-end bench against the new fixtures**

Run: `python3 -m pipeline.pipeline bench --no-tokens`
Expected: emits a summary table, appends one line to `bench/history.jsonl`, exits 0.

- [ ] **Step 10: Add an integration test that runs the real fixtures**

Append to `pipeline/tests/test_bench.py`:

```python
def test_real_fixtures_complete_successfully(tmp_path, monkeypatch):
    """All authored fixtures under bench/fixtures/ run without errors."""
    from pipeline.bench import run_mode_a

    repo_root = Path(__file__).resolve().parent.parent.parent
    fixtures_dir = repo_root / "bench" / "fixtures"
    if not fixtures_dir.is_dir():
        import pytest
        pytest.skip("fixtures directory not present")

    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    monkeypatch.chdir(repo_root)
    history = tmp_path / "history.jsonl"

    rc = run_mode_a(
        fixtures_dir=fixtures_dir,
        history_path=history,
        use_api=False,
        iterations=2,
        skip_cold_start=True,
    )
    assert rc == 0
    record = json.loads(history.read_text().strip().splitlines()[-1])
    names = {f["name"] for f in record["fixtures"]}
    assert len(names) >= 8
    # Auto-generated skip fixture should short-circuit (wall time near zero).
    skip_fx = next(
        f for f in record["fixtures"] if "auto-generated-skip" in f["name"]
    )
    assert skip_fx["wall_ms_p50"] < 10.0
```

- [ ] **Step 11: Run the integration test**

Run: `pytest pipeline/tests/test_bench.py::test_real_fixtures_complete_successfully -v`
Expected: PASS.

- [ ] **Step 12: Commit**

```bash
git add bench/fixtures pipeline/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add 8 bench fixtures covering script/ast/semantic engines

Fixtures exercise: single-engine smoke (01-03), mixed-engine path (04),
multi-level extends chain (05), payload size scaling (06, 07), and the
auto-generated skip short-circuit (08). Integration test runs all of
them against run_mode_a.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Document the bench in README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Read the current README tail**

Run: `wc -l README.md`
Record the final line number; the new section appends at end.

- [ ] **Step 2: Append a bench section**

Append to `README.md`:

```markdown

## Test Bench

Bully ships with a local bench for watching its own speed and input-token cost over time. Two modes:

### Mode A — fixture suite (regression trend)

```bash
bully bench                    # run all bench/fixtures/, append to bench/history.jsonl
bully bench --compare          # diff the last two runs
bully bench --no-tokens        # skip Anthropic API call, use char-count proxy
bully bench --json             # emit the raw run record on stdout
```

Results are written to `bench/history.jsonl`, one line per run. Commit a fresh run alongside changes that touch `pipeline/pipeline.py` to make speed/token impact visible in PRs.

### Mode B — config cost analysis

```bash
bully bench --config path/to/.bully.yml
```

Reports the input-token cost of the given config per invocation: floor tokens, per-rule marginal cost (sorted), diff scaling at 1/10/100/1000 added lines, and per-scope grouping. Useful for deciding whether a rule or rule pack earns its keep.

### Real token counts

Both modes use Anthropic's `messages/count_tokens` endpoint when `ANTHROPIC_API_KEY` is set and the optional `anthropic` SDK is installed (`pip install -e ".[bench]"`). Without either, both modes fall back to a `len(json.dumps(payload))` proxy and tag the output `method: proxy`.

The bench does not make real model calls — only `count_tokens`, which is free and does not spend credits.
```

- [ ] **Step 3: Verify the README renders coherently**

Run: `tail -50 README.md`
Expected: the bench section is present and the preceding section still closes cleanly.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Document the bench in README

Covers Mode A (fixture suite + history.jsonl), Mode B (config cost
analysis), and the ANTHROPIC_API_KEY / optional anthropic SDK
requirement for real token counts.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Checklist

Before marking this plan complete:

**Spec coverage:**
- [x] Two modes (A fixture, B config) — Tasks 6, 7, 8
- [x] Fixture format (config.yml + fixture.json) — Task 9
- [x] Phase-timer hook in pipeline — Task 1
- [x] Token counting with count_tokens + proxy fallback — Task 4
- [x] History JSONL with git sha, python version, aggregates — Task 6
- [x] --compare mode — Task 7
- [x] --no-tokens / --json flags — Tasks 2, 6, 8
- [x] 8 hand-authored fixtures — Task 9
- [x] Optional anthropic dep in pyproject — Task 2
- [x] Trust-gate bypass via `BULLY_TRUST_ALL` env — used in Tasks 5, 6, 9
- [x] Failure modes (missing config, missing fixture, no API key) — Tasks 3, 4, 6, 8

**Placeholder scan:** no TBD/TODO/"add appropriate error handling"/"similar to above" patterns. All code is spelled out.

**Type consistency:** `Fixture` dataclass defined in Task 3 is used with the same field names in Tasks 5, 6, 9. `count_tokens` signature `(payload, *, system, use_api)` is consistent in Tasks 4, 5, 8.

**Scope:** Single implementation plan. No decomposition needed.
