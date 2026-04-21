"""Tests for pipeline.rule_runner — per-rule execution + thread pool driver."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rule_runner import RuleContext, RuleResult


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


from rule_runner import evaluate_rule

from pipeline import Rule, Violation


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
    violation = Violation(rule="r1", engine="script", severity="error", line=12, description="bad")
    result = evaluate_rule(rule, ctx, "script", executor_fn=lambda r, c: [violation])
    assert len(result.violations) == 1
    assert result.violations[0].line == 12
    assert result.record["verdict"] == "violation"
    assert result.record["line"] == 12


def test_evaluate_rule_propagates_fix_hint():
    rule = _make_rule(fix_hint="use foo() instead")
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    violation = Violation(rule="r1", engine="script", severity="error", line=5, description="bad")
    result = evaluate_rule(rule, ctx, "script", executor_fn=lambda r, c: [violation])
    assert result.violations[0].suggestion == "use foo() instead"


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


def test_evaluate_rule_truncates_long_exception_messages_to_500_chars():
    rule = _make_rule()

    def boom(r, c):
        raise RuntimeError("x" * 600)

    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    result = evaluate_rule(rule, ctx, "script", executor_fn=boom)

    assert len(result.violations) == 1
    description = result.violations[0].description
    assert len(description) == 500
    assert description.startswith("internal error: RuntimeError:")


import time as _time

from rule_runner import run_rules_parallel


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
    results = run_rules_parallel(rules, ctx, "script", _delayed_executor(delays), max_workers=4)
    assert [r.rule_id for r in results] == ["r0", "r1", "r2", "r3"]


def test_run_rules_parallel_actually_parallel():
    import threading as _threading

    rules = [_make_rule(f"r{i}") for i in range(4)]
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)

    active = 0
    peak = 0
    lock = _threading.Lock()
    barrier = _threading.Event()

    def fn(rule, c):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        # Wait until all 4 workers have entered before any of them leave.
        # First thread to reach peak=4 trips the barrier for everyone.
        if not barrier.is_set() and peak >= 4:
            barrier.set()
        barrier.wait(timeout=1.0)
        with lock:
            active -= 1
        return []

    run_rules_parallel(rules, ctx, "script", fn, max_workers=4)
    assert peak == 4, f"expected peak=4 concurrent workers, got peak={peak}"


def test_run_rules_parallel_max_workers_1_serializes():
    rules = [_make_rule(f"r{i}") for i in range(3)]
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    delays = {f"r{i}": 0.1 for i in range(3)}  # serial ~0.3s
    t0 = _time.perf_counter()
    run_rules_parallel(rules, ctx, "script", _delayed_executor(delays), max_workers=1)
    elapsed = _time.perf_counter() - t0
    assert elapsed >= 0.28, f"expected serial ~0.3s, got {elapsed:.2f}s"


def test_run_rules_parallel_single_rule_skips_the_pool():
    # Fast path: one rule must execute on the calling thread, not a worker.
    import threading as _threading

    rules = [_make_rule("r0")]
    ctx = RuleContext(file_path="f.py", diff="", baseline={}, config_path=None)
    calling_thread = _threading.current_thread()
    observed: dict[str, _threading.Thread] = {}

    def fn(rule, c):
        observed["thread"] = _threading.current_thread()
        return []

    results = run_rules_parallel(rules, ctx, "script", fn, max_workers=8)
    assert len(results) == 1
    assert results[0].rule_id == "r0"
    assert observed["thread"] is calling_thread, (
        f"expected fast path to run inline on calling thread "
        f"({calling_thread.name}), got {observed['thread'].name}"
    )


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
