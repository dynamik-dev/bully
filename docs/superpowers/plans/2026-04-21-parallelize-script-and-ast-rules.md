# Parallelize Script and AST Rule Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Speed up the PostToolUse hook by running script and AST rules concurrently inside `pipeline.py` while preserving rule-declaration ordering, per-rule exception isolation, and blocking severity for internal errors.

**Architecture:** Extract per-rule execution from `pipeline.py`'s `_run_deterministic` closure into a new `pipeline/rule_runner.py` module owning two dataclasses (`RuleContext`, `RuleResult`), a pure `evaluate_rule()` function, and a `ThreadPoolExecutor`-backed `run_rules_parallel()` driver that returns results in submission order. `pipeline.py` folds the results onto the main thread, preserving current shared-state semantics. A new `execution.max_workers` key in `.bully.yml` plus `BULLY_MAX_WORKERS` env var control concurrency (default `min(8, os.cpu_count() or 4)`).

**Tech Stack:** Python 3.10+ stdlib (`concurrent.futures`, `dataclasses`, `threading`). No new dependencies.

**Spec reference:** `docs/superpowers/specs/2026-04-21-parallelize-script-and-ast-rules-design.md`

---

## File Map

- **Create** `pipeline/rule_runner.py` — `RuleContext`, `RuleResult`, `evaluate_rule`, `run_rules_parallel`.
- **Create** `pipeline/tests/test_rule_runner.py` — unit tests for the new module.
- **Modify** `pipeline/pipeline.py`:
  - Add `"execution"` to `VALID_TOP_LEVEL` (line 42) and parse its nested block in `_parse_single_file` (around line 373).
  - Extend `_ParsedConfig` (line 250) with `max_workers: int | None = None`.
  - Add a public `resolve_max_workers(config_path)` helper after `parse_config` (around line 578).
  - Replace the `_run_deterministic` closure invocations in `run_pipeline` (lines 1735-1766) with calls to `run_rules_parallel` + a main-thread fold.
- **Modify** `pipeline/tests/test_pipeline.py` — integration test for the hook path wall-time.
- **Modify** `pipeline/tests/test_parser.py` (or add `test_parser_execution.py` if cleaner) — config parser tests for the `execution:` block.
- **Create** `pipeline/tests/fixtures/parallel-config.yml` — fixture with four `sleep 0.3` script rules.

---

## Task 1: Create the `rule_runner` module skeleton with dataclasses

**Files:**
- Create: `pipeline/rule_runner.py`
- Create: `pipeline/tests/test_rule_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# pipeline/tests/test_rule_runner.py
"""Tests for pipeline.rule_runner — per-rule execution + thread pool driver."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.rule_runner import RuleContext, RuleResult


def test_rule_context_carries_expected_fields():
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path="/tmp/.bully.yml")
    assert ctx.file_path == "f.py"
    assert ctx.diff == ""
    assert ctx.baseline == {}
    assert ctx.config_path == "/tmp/.bully.yml"


def test_rule_result_defaults():
    result = RuleResult(rule_id="r1", violations=[], record={"id": "r1"})
    assert result.rule_id == "r1"
    assert result.violations == []
    assert result.record == {"id": "r1"}
    assert result.internal_error is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest pipeline/tests/test_rule_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.rule_runner'`

- [ ] **Step 3: Write minimal implementation**

```python
# pipeline/rule_runner.py
"""Per-rule execution helpers for pipeline.py.

Extracted from the pipeline.py `_run_deterministic` closure so rule
evaluation can be parallelized via a ThreadPoolExecutor while keeping
the main-thread fold (violation/record collection) deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class RuleContext:
    """Per-file context passed to every rule evaluator.

    Intentionally frozen + immutable so it is safe to share across worker
    threads. Nothing inside should be mutated.
    """

    file_path: str
    diff: str
    baseline: dict  # keys are (rule_id, rel_path, line, checksum) tuples
    config_path: str | None


@dataclass
class RuleResult:
    """Output of a single rule evaluation, ready for main-thread fold."""

    rule_id: str
    violations: list  # list[Violation] — typed loosely to avoid import cycle
    record: dict
    internal_error: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest pipeline/tests/test_rule_runner.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add pipeline/rule_runner.py pipeline/tests/test_rule_runner.py
git commit -m "Add RuleContext and RuleResult dataclasses in rule_runner module"
```

---

## Task 2: Implement `evaluate_rule` — passing and violating paths

**Files:**
- Modify: `pipeline/rule_runner.py`
- Modify: `pipeline/tests/test_rule_runner.py`

- [ ] **Step 1: Add tests for the happy paths**

Append to `pipeline/tests/test_rule_runner.py`:

```python
from pipeline import Rule, Violation
from pipeline.rule_runner import evaluate_rule


def _make_rule(rid="r1", severity="error", fix_hint=None):
    return Rule(
        id=rid,
        description="test rule",
        engine="script",
        scope="*",
        severity=severity,
        script="true",
        fix_hint=fix_hint,
    )


def test_evaluate_rule_pass_path():
    rule = _make_rule()
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    result = evaluate_rule(rule, ctx, "script", executor_fn=lambda r, c: [])
    assert result.rule_id == "r1"
    assert result.violations == []
    assert result.record["id"] == "r1"
    assert result.record["engine"] == "script"
    assert result.record["verdict"] == "pass"
    assert result.record["severity"] == "error"
    assert isinstance(result.record["latency_ms"], int)
    assert result.internal_error is False


def test_evaluate_rule_violation_path():
    rule = _make_rule()
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    violation = Violation(
        rule="r1", engine="script", severity="error", line=12, description="bad"
    )
    result = evaluate_rule(rule, ctx, "script", executor_fn=lambda r, c: [violation])
    assert len(result.violations) == 1
    assert result.violations[0].line == 12
    assert result.record["verdict"] == "violation"
    assert result.record["line"] == 12


def test_evaluate_rule_propagates_fix_hint():
    rule = _make_rule(fix_hint="use foo() instead")
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    violation = Violation(
        rule="r1", engine="script", severity="error", line=5, description="bad"
    )
    result = evaluate_rule(rule, ctx, "script", executor_fn=lambda r, c: [violation])
    assert result.violations[0].suggestion == "use foo() instead"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_rule_runner.py -v`
Expected: FAIL with `ImportError: cannot import name 'evaluate_rule'`

- [ ] **Step 3: Implement `evaluate_rule` (minus exception handling — that's Task 3)**

Add to `pipeline/rule_runner.py`:

```python
import time
from dataclasses import replace

from pipeline import Rule, Violation
from pipeline import _is_baselined, _line_has_disable


def evaluate_rule(
    rule: Rule,
    ctx: RuleContext,
    engine: str,
    executor_fn: Callable[[Rule, RuleContext], list[Violation]],
) -> RuleResult:
    """Run one rule against one file and return a ready-to-fold RuleResult.

    `executor_fn` is the engine-specific runner (e.g. a lambda wrapping
    execute_script_rule or execute_ast_rule). It is called with the rule
    and ctx; it returns raw Violations. This helper then applies
    fix_hint, line-disable filtering, and baseline filtering, and builds
    the rule_records entry matching pipeline.py's historical shape.
    """
    start = time.perf_counter()
    violations = executor_fn(rule, ctx)
    latency_ms = int((time.perf_counter() - start) * 1000)

    if rule.fix_hint:
        violations = [
            replace(v, suggestion=v.suggestion or rule.fix_hint) for v in violations
        ]

    filtered: list[Violation] = []
    for v in violations:
        if _line_has_disable(ctx.file_path, v.line, rule.id):
            continue
        if _is_baselined(ctx.baseline, rule.id, ctx.config_path, ctx.file_path, v.line):
            continue
        filtered.append(v)

    if filtered:
        record = {
            "id": rule.id,
            "engine": engine,
            "verdict": "violation",
            "severity": rule.severity,
            "line": filtered[0].line,
            "latency_ms": latency_ms,
        }
    else:
        record = {
            "id": rule.id,
            "engine": engine,
            "verdict": "pass",
            "severity": rule.severity,
            "latency_ms": latency_ms,
        }

    return RuleResult(rule_id=rule.id, violations=filtered, record=record)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest pipeline/tests/test_rule_runner.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add pipeline/rule_runner.py pipeline/tests/test_rule_runner.py
git commit -m "Implement evaluate_rule pass/violation paths with fix_hint + filters"
```

---

## Task 3: Implement exception isolation inside `evaluate_rule`

**Files:**
- Modify: `pipeline/rule_runner.py`
- Modify: `pipeline/tests/test_rule_runner.py`

- [ ] **Step 1: Add failing test**

Append to `pipeline/tests/test_rule_runner.py`:

```python
def test_evaluate_rule_isolates_exceptions_as_blocking_violation():
    rule = _make_rule(severity="warning")  # prove severity gets overridden to error

    def boom(r, c):
        raise RuntimeError("kaboom")

    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    result = evaluate_rule(rule, ctx, "script", executor_fn=boom)

    assert result.internal_error is True
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.rule == "r1"
    assert v.engine == "script"
    assert v.severity == "error"  # blocking, regardless of rule.severity
    assert v.description.startswith("internal error: RuntimeError")
    assert "kaboom" in v.description
    assert result.record["verdict"] == "violation"
    assert result.record["error"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest pipeline/tests/test_rule_runner.py::test_evaluate_rule_isolates_exceptions_as_blocking_violation -v`
Expected: FAIL (the lambda raises and bubbles out of `evaluate_rule`)

- [ ] **Step 3: Wrap the body of `evaluate_rule` in a try/except**

Replace the body of `evaluate_rule` in `pipeline/rule_runner.py`:

```python
def evaluate_rule(
    rule: Rule,
    ctx: RuleContext,
    engine: str,
    executor_fn: Callable[[Rule, RuleContext], list[Violation]],
) -> RuleResult:
    """Run one rule against one file and return a ready-to-fold RuleResult.

    Exceptions raised by executor_fn or the filters are caught and
    converted to a single blocking Violation so one bad rule cannot
    take down the rest of the run. KeyboardInterrupt and SystemExit
    are intentionally not caught.
    """
    start = time.perf_counter()
    try:
        violations = executor_fn(rule, ctx)

        if rule.fix_hint:
            violations = [
                replace(v, suggestion=v.suggestion or rule.fix_hint) for v in violations
            ]

        filtered: list[Violation] = []
        for v in violations:
            if _line_has_disable(ctx.file_path, v.line, rule.id):
                continue
            if _is_baselined(
                ctx.baseline, rule.id, ctx.config_path, ctx.file_path, v.line
            ):
                continue
            filtered.append(v)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 — intentional broad catch for isolation
        latency_ms = int((time.perf_counter() - start) * 1000)
        description = f"internal error: {type(exc).__name__}: {exc}"[:500]
        err_violation = Violation(
            rule=rule.id,
            engine=engine,
            severity="error",
            line=None,
            description=description,
        )
        record = {
            "id": rule.id,
            "engine": engine,
            "verdict": "violation",
            "severity": "error",
            "line": None,
            "latency_ms": latency_ms,
            "error": True,
        }
        return RuleResult(
            rule_id=rule.id,
            violations=[err_violation],
            record=record,
            internal_error=True,
        )

    latency_ms = int((time.perf_counter() - start) * 1000)
    if filtered:
        record = {
            "id": rule.id,
            "engine": engine,
            "verdict": "violation",
            "severity": rule.severity,
            "line": filtered[0].line,
            "latency_ms": latency_ms,
        }
    else:
        record = {
            "id": rule.id,
            "engine": engine,
            "verdict": "pass",
            "severity": rule.severity,
            "latency_ms": latency_ms,
        }

    return RuleResult(rule_id=rule.id, violations=filtered, record=record)
```

- [ ] **Step 4: Run all rule_runner tests to verify**

Run: `python -m pytest pipeline/tests/test_rule_runner.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add pipeline/rule_runner.py pipeline/tests/test_rule_runner.py
git commit -m "Isolate per-rule exceptions as blocking internal-error violations"
```

---

## Task 4: Implement `run_rules_parallel`

**Files:**
- Modify: `pipeline/rule_runner.py`
- Modify: `pipeline/tests/test_rule_runner.py`

- [ ] **Step 1: Add failing tests for ordering, parallelism, and isolation**

Append to `pipeline/tests/test_rule_runner.py`:

```python
import threading
import time as _time

from pipeline.rule_runner import run_rules_parallel


def _delayed_executor(delay_by_id: dict[str, float]):
    def fn(rule, ctx):
        _time.sleep(delay_by_id.get(rule.id, 0.0))
        return []
    return fn


def test_run_rules_parallel_preserves_declaration_order():
    rules = [_make_rule(f"r{i}") for i in range(4)]
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    # Staggered delays so completion order != declaration order.
    delays = {"r0": 0.12, "r1": 0.02, "r2": 0.08, "r3": 0.04}
    results = run_rules_parallel(
        rules, ctx, "script", _delayed_executor(delays), max_workers=4
    )
    assert [r.rule_id for r in results] == ["r0", "r1", "r2", "r3"]


def test_run_rules_parallel_actually_parallel():
    rules = [_make_rule(f"r{i}") for i in range(4)]
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    delays = {f"r{i}": 0.15 for i in range(4)}  # serial would be ~0.6s
    t0 = _time.perf_counter()
    run_rules_parallel(rules, ctx, "script", _delayed_executor(delays), max_workers=4)
    elapsed = _time.perf_counter() - t0
    assert elapsed < 0.45, f"expected <0.45s parallel, got {elapsed:.2f}s"


def test_run_rules_parallel_max_workers_1_serializes():
    rules = [_make_rule(f"r{i}") for i in range(3)]
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    delays = {f"r{i}": 0.1 for i in range(3)}  # serial ~0.3s
    t0 = _time.perf_counter()
    run_rules_parallel(rules, ctx, "script", _delayed_executor(delays), max_workers=1)
    elapsed = _time.perf_counter() - t0
    assert elapsed >= 0.28, f"expected serial ~0.3s, got {elapsed:.2f}s"


def test_run_rules_parallel_one_raising_rule_does_not_abort_others():
    rules = [_make_rule("r0"), _make_rule("r1"), _make_rule("r2")]
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)

    def fn(rule, c):
        if rule.id == "r1":
            raise RuntimeError("boom")
        return []

    results = run_rules_parallel(rules, ctx, "script", fn, max_workers=3)
    assert [r.rule_id for r in results] == ["r0", "r1", "r2"]
    assert results[0].internal_error is False
    assert results[1].internal_error is True
    assert results[2].internal_error is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_rule_runner.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_rules_parallel'`

- [ ] **Step 3: Implement `run_rules_parallel`**

Append to `pipeline/rule_runner.py`:

```python
from concurrent.futures import ThreadPoolExecutor


def run_rules_parallel(
    rules: list[Rule],
    ctx: RuleContext,
    engine: str,
    executor_fn: Callable[[Rule, RuleContext], list[Violation]],
    max_workers: int,
) -> list[RuleResult]:
    """Evaluate `rules` concurrently and return RuleResults in submission order.

    `evaluate_rule` is designed not to raise, but we still wrap future.result()
    in a best-effort try/except that synthesizes an internal-error RuleResult
    if something slips through (e.g. a buggy executor_fn somehow bypasses
    the inner guard). No future is cancelled on failure — every rule runs to
    completion so the user sees the full picture.
    """
    if not rules:
        return []
    # A pool with zero workers would deadlock on submit(); clamp to >=1.
    workers = max(1, min(max_workers, len(rules)))
    results: list[RuleResult | None] = [None] * len(rules)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bully-rule") as pool:
        futures = [
            pool.submit(evaluate_rule, rule, ctx, engine, executor_fn)
            for rule in rules
        ]
        for idx, fut in enumerate(futures):
            try:
                results[idx] = fut.result()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 — defence-in-depth
                rule = rules[idx]
                description = f"internal error: {type(exc).__name__}: {exc}"[:500]
                err_violation = Violation(
                    rule=rule.id,
                    engine=engine,
                    severity="error",
                    line=None,
                    description=description,
                )
                record = {
                    "id": rule.id,
                    "engine": engine,
                    "verdict": "violation",
                    "severity": "error",
                    "line": None,
                    "error": True,
                }
                results[idx] = RuleResult(
                    rule_id=rule.id,
                    violations=[err_violation],
                    record=record,
                    internal_error=True,
                )
    # mypy-friendly: all slots have been filled.
    return [r for r in results if r is not None]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest pipeline/tests/test_rule_runner.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add pipeline/rule_runner.py pipeline/tests/test_rule_runner.py
git commit -m "Add run_rules_parallel thread pool driver with order-preserving fold"
```

---

## Task 5: Parse `execution.max_workers` from `.bully.yml`

**Files:**
- Modify: `pipeline/pipeline.py:42` (`VALID_TOP_LEVEL`), `pipeline/pipeline.py:250` (`_ParsedConfig`), `pipeline/pipeline.py:260-449` (`_parse_single_file`), `pipeline/pipeline.py:581-612` (`_load_with_extends`)
- Create: `pipeline/tests/test_parser_execution.py`

- [ ] **Step 1: Write failing parser tests**

Create `pipeline/tests/test_parser_execution.py`:

```python
"""Tests for parsing the top-level `execution:` block in .bully.yml."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import ConfigError
from pipeline.pipeline import _parse_single_file


def _write(tmp_path, body):
    p = tmp_path / ".bully.yml"
    p.write_text(body)
    return str(p)


def test_execution_block_absent_yields_none(tmp_path):
    path = _write(tmp_path, "rules:\n  r1:\n    description: x\n    engine: script\n    scope: '*'\n    severity: error\n    script: 'true'\n")
    parsed = _parse_single_file(path)
    assert parsed.max_workers is None


def test_execution_block_sets_max_workers(tmp_path):
    body = (
        "execution:\n"
        "  max_workers: 4\n"
        "rules:\n"
        "  r1:\n"
        "    description: x\n"
        "    engine: script\n"
        "    scope: '*'\n"
        "    severity: error\n"
        "    script: 'true'\n"
    )
    parsed = _parse_single_file(_write(tmp_path, body))
    assert parsed.max_workers == 4


def test_execution_block_unknown_subkey_raises(tmp_path):
    body = "execution:\n  bogus: 1\n"
    with pytest.raises(ConfigError, match="unknown execution field"):
        _parse_single_file(_write(tmp_path, body))


def test_execution_block_non_positive_raises(tmp_path):
    body = "execution:\n  max_workers: 0\n"
    with pytest.raises(ConfigError, match="max_workers must be a positive integer"):
        _parse_single_file(_write(tmp_path, body))


def test_execution_block_non_integer_raises(tmp_path):
    body = "execution:\n  max_workers: abc\n"
    with pytest.raises(ConfigError, match="max_workers must be a positive integer"):
        _parse_single_file(_write(tmp_path, body))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_parser_execution.py -v`
Expected: FAIL (unknown top-level key 'execution')

- [ ] **Step 3: Extend the parser to recognize `execution:`**

Edit `pipeline/pipeline.py` line 42:

```python
VALID_TOP_LEVEL = {"rules", "schema_version", "extends", "skip", "execution"}
```

Edit `pipeline/pipeline.py` lines 250-257 — extend `_ParsedConfig`:

```python
@dataclass
class _ParsedConfig:
    """Internal structure returned by _parse_single_file."""

    rules: list[Rule] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    schema_version: int | None = None
    max_workers: int | None = None
```

Inside `_parse_single_file` (around line 274), add a new flag next to `in_skip_block`:

```python
    in_skip_block = False
    in_execution_block = False
    skip: list[str] = []
    max_workers: int | None = None
```

Immediately after the existing skip-block continuation block (around line 335), add an execution-block continuation:

```python
        # Execution-block continuation: `<key>: <value>` at indent 2.
        if in_execution_block and indent >= 2 and ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value_raw = value.strip()
            if key != "max_workers":
                raise ConfigError(
                    f"unknown execution field '{key}' "
                    f"(allowed: max_workers)",
                    lineno,
                )
            parsed_val = _parse_scalar(value_raw)
            try:
                n = int(parsed_val)
                if n <= 0:
                    raise ValueError
            except (TypeError, ValueError) as e:
                raise ConfigError(
                    f"max_workers must be a positive integer, got {parsed_val!r}",
                    lineno,
                ) from e
            max_workers = n
            continue
        elif in_execution_block:
            in_execution_block = False
```

Inside the top-level key handler (after the `skip` branch around line 383), add:

```python
            elif key == "execution":
                if value_raw != "":
                    raise ConfigError(
                        "execution must be followed by an indented block",
                        lineno,
                    )
                in_execution_block = True
```

Finally, update the tail `return _ParsedConfig(...)` (around line 444) to include `max_workers=max_workers`.

Also update `_load_with_extends` (around line 581-612) so that `max_workers` from the top-level config propagates. Add after `parsed = _parse_single_file(path)`:

Nothing needed here — `_load_with_extends` currently returns `list[Rule]`. The `max_workers` value is read separately via the new `resolve_max_workers` helper added in Task 6, which re-parses the entrypoint config only (extends do not supply execution settings).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest pipeline/tests/test_parser_execution.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run full parser test suite to confirm no regressions**

Run: `python -m pytest pipeline/tests/test_parser.py pipeline/tests/test_parser_hardening.py pipeline/tests/test_parser_properties.py -v`
Expected: PASS (all existing tests)

- [ ] **Step 6: Commit**

```bash
git add pipeline/pipeline.py pipeline/tests/test_parser_execution.py
git commit -m "Parse execution.max_workers block in .bully.yml config"
```

---

## Task 6: Add `resolve_max_workers` helper

**Files:**
- Modify: `pipeline/pipeline.py` (add helper after `parse_config`, around line 578)
- Modify: `pipeline/tests/test_parser_execution.py` (add env + default tests)

- [ ] **Step 1: Write failing resolver tests**

Append to `pipeline/tests/test_parser_execution.py`:

```python
import os

from pipeline.pipeline import resolve_max_workers


def test_resolve_max_workers_default_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("BULLY_MAX_WORKERS", raising=False)
    path = _write(tmp_path, "rules:\n")
    expected = min(8, os.cpu_count() or 4)
    assert resolve_max_workers(path) == expected


def test_resolve_max_workers_uses_config_value(tmp_path, monkeypatch):
    monkeypatch.delenv("BULLY_MAX_WORKERS", raising=False)
    body = "execution:\n  max_workers: 3\n"
    assert resolve_max_workers(_write(tmp_path, body)) == 3


def test_resolve_max_workers_env_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BULLY_MAX_WORKERS", "16")
    body = "execution:\n  max_workers: 3\n"
    assert resolve_max_workers(_write(tmp_path, body)) == 16


def test_resolve_max_workers_env_invalid_falls_back_to_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BULLY_MAX_WORKERS", "nope")
    body = "execution:\n  max_workers: 3\n"
    assert resolve_max_workers(_write(tmp_path, body)) == 3


def test_resolve_max_workers_env_zero_falls_back_to_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BULLY_MAX_WORKERS", "0")
    body = "execution:\n  max_workers: 3\n"
    assert resolve_max_workers(_write(tmp_path, body)) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_parser_execution.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_max_workers'`

- [ ] **Step 3: Implement `resolve_max_workers`**

Add to `pipeline/pipeline.py` after `parse_config` (around line 578):

```python
def resolve_max_workers(config_path: str) -> int:
    """Resolve concurrent-rule worker count.

    Precedence (highest first):
      1. BULLY_MAX_WORKERS env var (positive int)
      2. execution.max_workers in the top-level .bully.yml
      3. Default: min(8, os.cpu_count() or 4)

    Invalid env values (non-int, zero, negative) silently fall through
    to the config / default. Config-level invalid values were already
    rejected at parse time by _parse_single_file.
    """
    env_raw = os.environ.get("BULLY_MAX_WORKERS")
    if env_raw is not None:
        try:
            n = int(env_raw)
            if n > 0:
                return n
        except ValueError:
            pass
    try:
        parsed = _parse_single_file(config_path)
        if parsed.max_workers is not None:
            return parsed.max_workers
    except ConfigError:
        pass  # trust the caller to surface parse errors elsewhere
    return min(8, os.cpu_count() or 4)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest pipeline/tests/test_parser_execution.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline.py pipeline/tests/test_parser_execution.py
git commit -m "Add resolve_max_workers helper with env/config/default precedence"
```

---

## Task 7: Wire `run_rules_parallel` into `run_pipeline`

**Files:**
- Modify: `pipeline/pipeline.py:1735-1766` (replace `_run_deterministic` loops)

- [ ] **Step 1: Replace the script and AST execution loops**

Locate `pipeline/pipeline.py` lines 1735-1766. Replace them with:

```python
    from pipeline.rule_runner import RuleContext, run_rules_parallel  # noqa: PLC0415

    max_workers = resolve_max_workers(config_path)
    ctx = RuleContext(
        file_path=file_path,
        diff=diff,
        baseline=baseline,
        config_path=config_path,
    )

    def _adapter_script(rule, rctx):
        return execute_script_rule(rule, rctx.file_path, rctx.diff)

    def _adapter_ast(rule, rctx):
        return execute_ast_rule(rule, rctx.file_path)

    def _fold(results):
        for result in results:
            filtered = [v for v in result.violations]
            if filtered:
                all_violations.extend(filtered)
            else:
                passed_checks.append(result.rule_id)
            rule_records.append(result.record)

    with phase_timer("script_exec"):
        if script_rules:
            _fold(
                run_rules_parallel(
                    script_rules, ctx, "script", _adapter_script, max_workers
                )
            )

    with phase_timer("ast_exec"):
        if ast_rules:
            if ast_grep_available():
                _fold(
                    run_rules_parallel(
                        ast_rules, ctx, "ast", _adapter_ast, max_workers
                    )
                )
            else:
                sys.stderr.write(
                    "bully: engine:ast rules matched but ast-grep not on PATH; skipping. "
                    f"{_AST_GREP_INSTALL_HINT}\n"
                )
                for rule in ast_rules:
                    rule_records.append(
                        {
                            "id": rule.id,
                            "engine": "ast",
                            "verdict": "skipped",
                            "severity": rule.severity,
                            "reason": "ast-grep-not-installed",
                        }
                    )
```

Delete the old nested `_run_deterministic` closure (lines 1690-1733) — it is no longer called. Leave `all_violations`, `passed_checks`, and `baseline` initialization in place.

- [ ] **Step 2: Run the full pipeline test suite**

Run: `python -m pytest pipeline/tests/test_pipeline.py -v`
Expected: PASS (all existing assertions)

- [ ] **Step 3: Run the full test suite to catch regressions**

Run: `python -m pytest pipeline/tests/ -v`
Expected: PASS (no regressions anywhere)

- [ ] **Step 4: Commit**

```bash
git add pipeline/pipeline.py
git commit -m "Wire run_rules_parallel into run_pipeline for script and AST phases"
```

---

## Task 8: Integration test — hook path wall-time

**Files:**
- Create: `pipeline/tests/fixtures/parallel-config.yml`
- Modify: `pipeline/tests/test_pipeline.py` (append integration test)

- [ ] **Step 1: Create the multi-rule fixture**

Create `pipeline/tests/fixtures/parallel-config.yml`:

```yaml
rules:
  sleep-a:
    description: "Sleeps 0.3s then passes"
    engine: script
    scope: "*.py"
    severity: warning
    script: "sleep 0.3 && exit 0"
  sleep-b:
    description: "Sleeps 0.3s then passes"
    engine: script
    scope: "*.py"
    severity: warning
    script: "sleep 0.3 && exit 0"
  sleep-c:
    description: "Sleeps 0.3s then passes"
    engine: script
    scope: "*.py"
    severity: warning
    script: "sleep 0.3 && exit 0"
  sleep-d:
    description: "Sleeps 0.3s then passes"
    engine: script
    scope: "*.py"
    severity: warning
    script: "sleep 0.3 && exit 0"
```

Also create a matching fixture source file `pipeline/tests/fixtures/parallel-target.py`:

```python
# empty file — rules don't inspect contents, they just sleep
```

- [ ] **Step 2: Write the failing integration test**

Append to `pipeline/tests/test_pipeline.py`:

```python
import time as _time


def test_parallel_script_rules_finish_under_serial_time(tmp_path, monkeypatch):
    # 4 rules × 0.3s sleep. Serial would be ~1.2s. Parallel should be ~0.35s.
    monkeypatch.delenv("BULLY_MAX_WORKERS", raising=False)
    t0 = _time.perf_counter()
    result = run_pipeline(
        str(FIXTURES / "parallel-config.yml"),
        str(FIXTURES / "parallel-target.py"),
        "",
    )
    elapsed = _time.perf_counter() - t0
    assert result["status"] == "pass"
    assert elapsed < 0.9, f"parallel wall time {elapsed:.2f}s exceeded threshold"


def test_parallel_rule_records_preserve_declaration_order(tmp_path, monkeypatch):
    monkeypatch.delenv("BULLY_MAX_WORKERS", raising=False)
    result = run_pipeline(
        str(FIXTURES / "parallel-config.yml"),
        str(FIXTURES / "parallel-target.py"),
        "",
        include_skipped=True,
    )
    rule_order = [
        r["rule"] if "rule" in r else r["id"]
        for r in result.get("rules_evaluated", [])
        if r.get("engine") == "script"
    ]
    assert rule_order == ["sleep-a", "sleep-b", "sleep-c", "sleep-d"]
```

- [ ] **Step 3: Run the integration tests**

Run: `python -m pytest pipeline/tests/test_pipeline.py::test_parallel_script_rules_finish_under_serial_time pipeline/tests/test_pipeline.py::test_parallel_rule_records_preserve_declaration_order -v`
Expected: PASS (2 passed). If the wall-time assertion fails, rerun once — CI jitter on first run can spike; persistent failure is a real regression.

- [ ] **Step 4: Commit**

```bash
git add pipeline/tests/fixtures/parallel-config.yml pipeline/tests/fixtures/parallel-target.py pipeline/tests/test_pipeline.py
git commit -m "Integration test: parallel script rules beat serial wall time"
```

---

## Task 9: Document the new config + env knob

**Files:**
- Modify: `README.md` (find the `.bully.yml` reference section and append an `execution:` example)
- Modify: `CHANGELOG.md` (add unreleased entry)

- [ ] **Step 1: Locate the README section for `.bully.yml` configuration**

Run: `grep -n "^##" README.md | head -40`
Identify the section describing `.bully.yml` top-level keys (typically "Configuration" or similar).

- [ ] **Step 2: Add the `execution:` subsection**

Append beneath the existing config documentation, inside the identified section:

```markdown
### Parallelism

bully evaluates script and AST rules concurrently inside a single file. By default it uses `min(8, os.cpu_count() or 4)` workers. Override via config:

```yaml
execution:
  max_workers: 4
```

Or via env (wins over config):

```
BULLY_MAX_WORKERS=2 git commit
```

Set `max_workers: 1` to restore fully serial execution if a rule script has side effects that require exclusive access to a resource.
```

- [ ] **Step 3: Add a CHANGELOG entry**

At the top of `CHANGELOG.md`, add (or extend) the unreleased section:

```markdown
## Unreleased

### Added
- Parallel execution of script and AST rules within a single file (`execution.max_workers` config, `BULLY_MAX_WORKERS` env). Default `min(8, os.cpu_count() or 4)`.
- Per-rule exception isolation: a rule that raises now emits a blocking internal-error violation, and other rules still run to completion.
```

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "Document execution.max_workers config and BULLY_MAX_WORKERS env var"
```

---

## Task 10: Final verification — full suite + lint

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest pipeline/tests/ -v`
Expected: PASS (entire suite, including the new tests from Tasks 1-8)

- [ ] **Step 2: Run ruff (project uses it per recent commits)**

Run: `ruff check pipeline/`
Expected: no new violations introduced

- [ ] **Step 3: Sanity-run the hook against the fixture**

Run: `python -m pipeline.pipeline --config pipeline/tests/fixtures/parallel-config.yml --file pipeline/tests/fixtures/parallel-target.py --diff "" 2>&1 | head -20`
Expected: `status: pass` (or whatever the CLI's equivalent success output is — confirm against the existing CLI behavior)

- [ ] **Step 4: Final commit if anything was adjusted**

If steps 1-3 produced any polish changes:

```bash
git add -A
git commit -m "Post-verification polish from parallel-rules rollout"
```

Otherwise skip this step.

---

## Self-Review Summary

- **Spec coverage** — every section of the spec (architecture, components, config, data flow, error handling, testing, rollout) maps to one or more tasks above. Non-goals (combined `ast-grep scan`, cross-file parallelism) are not implemented, matching the spec.
- **Placeholder scan** — no TBDs, no "add appropriate error handling", every code block is complete.
- **Type consistency** — `RuleContext`, `RuleResult`, `evaluate_rule`, `run_rules_parallel` signatures and field names are identical across Tasks 1, 2, 3, 4, 7. `max_workers` field name, `execution.max_workers` config key, and `BULLY_MAX_WORKERS` env var are consistent across Tasks 5, 6, 7, 9.
- **Fold semantics** — the main-thread fold in Task 7 replicates the exact shape `_run_deterministic` produced (appends to `all_violations` on violation, `passed_checks` on pass, always appends `record`).
