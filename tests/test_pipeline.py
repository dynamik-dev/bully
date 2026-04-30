"""Tests for semantic payload builder and full pipeline orchestration."""

import time as _time
from pathlib import Path

from bully import Rule, build_semantic_payload_dict, run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"

# Parallelism integration test: 4 rules x 0.3s sleep = 1.2s serial.
# Parallel + subprocess spawn overhead caps at PARALLEL_MAX_SECONDS.
# Lower bound guards against a mocked clock silently passing the test.
_PARALLEL_SLEEP_SECONDS = 0.3
_PARALLEL_MAX_SECONDS = 1.5
_PARALLEL_MIN_SECONDS = _PARALLEL_SLEEP_SECONDS * 0.9


def test_payload_includes_file_and_diff():
    rules = [
        Rule(
            id="test-rule",
            description="Test description",
            engine="semantic",
            scope="*",
            severity="error",
        ),
    ]
    payload = build_semantic_payload_dict("test.php", "+ new line", ["no-compact"], rules)
    assert payload["file"] == "test.php"
    assert payload["diff"] == "+ new line"


def test_payload_includes_passed_checks():
    rules = [
        Rule(id="test-rule", description="Test", engine="semantic", scope="*", severity="error"),
    ]
    payload = build_semantic_payload_dict("test.php", "diff", ["no-compact", "no-db-facade"], rules)
    assert payload["passed_checks"] == ["no-compact", "no-db-facade"]


def test_payload_includes_semantic_rules():
    rules = [
        Rule(id="rule-a", description="First rule", engine="semantic", scope="*", severity="error"),
        Rule(
            id="rule-b", description="Second rule", engine="semantic", scope="*", severity="warning"
        ),
    ]
    payload = build_semantic_payload_dict("test.php", "diff", [], rules)
    assert len(payload["evaluate"]) == 2
    assert payload["evaluate"][0]["id"] == "rule-a"
    assert payload["evaluate"][0]["description"] == "First rule"
    assert payload["evaluate"][1]["severity"] == "warning"


def test_pipeline_no_matching_rules_passes():
    result = run_pipeline(str(FIXTURES / "basic-config.yml"), "test.md", "some diff")
    assert result["status"] == "pass"


def test_pipeline_script_violation_blocks():
    result = run_pipeline(
        str(FIXTURES / "basic-config.yml"),
        str(FIXTURES / "violation.php"),
        "",
    )
    assert result["status"] == "blocked"
    assert len(result["violations"]) >= 1
    assert result["violations"][0]["rule"] == "no-compact"


def test_pipeline_clean_file_produces_semantic_payload():
    diff = (
        "--- a/clean.php\n"
        "+++ b/clean.php\n"
        "@@ -10,2 +10,3 @@\n"
        "+    $result = User::query()->get();\n"
        "+    return ['users' => $result];\n"
    )
    result = run_pipeline(
        str(FIXTURES / "basic-config.yml"),
        str(FIXTURES / "clean.php"),
        diff,
    )
    assert result["status"] == "evaluate"
    assert "no-compact" in result["passed_checks"]
    assert len(result["evaluate"]) >= 1


def test_pipeline_single_line_semantic_edit_dispatches():
    """A single-line addition of a real semantic violation must dispatch.

    Pinned to a one-line diff: this would have been skipped under the old
    `len(added) < 2` heuristic (the diff has exactly one added line and no
    removed lines, so it doesn't pass any other gate). Confirms the
    relaxation -- one-line introductions like `eval(input)` are exactly
    what semantic rules should catch.
    """
    diff = (
        "--- a/clean.php\n"
        "+++ b/clean.php\n"
        "@@ -13,1 +13,2 @@\n"
        "+        $unused = $this->buildResult();\n"
    )
    result = run_pipeline(
        str(FIXTURES / "basic-config.yml"),
        str(FIXTURES / "clean.php"),
        diff,
    )
    assert result["status"] == "evaluate", (
        f"single-line semantic edit should dispatch; got {result.get('status')!r} "
        f"with semantic_skipped={result.get('semantic_skipped')!r}"
    )
    rule_ids = [r["id"] for r in result["evaluate"]]
    assert "inline-single-use-vars" in rule_ids, (
        f"expected the semantic rule to be in evaluate list, got {rule_ids!r}"
    )


def test_pipeline_script_block_skips_semantic():
    result = run_pipeline(
        str(FIXTURES / "basic-config.yml"),
        str(FIXTURES / "violation.php"),
        "",
    )
    assert result["status"] == "blocked"
    assert "evaluate" not in result


def test_parallel_script_rules_finish_under_serial_time(monkeypatch):
    # Serial would be ~4 * 0.3s = 1.2s. Parallel + subprocess startup should
    # fit under _PARALLEL_MAX_SECONDS even on slow CI runners.
    monkeypatch.setenv("BULLY_MAX_WORKERS", "4")
    t0 = _time.perf_counter()
    result = run_pipeline(
        str(FIXTURES / "parallel-config.yml"),
        str(FIXTURES / "parallel-target.py"),
        "",
    )
    elapsed = _time.perf_counter() - t0
    assert result["status"] == "pass"
    assert elapsed < _PARALLEL_MAX_SECONDS, (
        f"parallel wall time {elapsed:.2f}s exceeded threshold {_PARALLEL_MAX_SECONDS}s"
    )
    assert elapsed >= _PARALLEL_MIN_SECONDS, (
        f"wall time {elapsed:.2f}s below sleep floor — mocked clock?"
    )


def test_parallel_rule_records_preserve_declaration_order(monkeypatch):
    monkeypatch.setenv("BULLY_MAX_WORKERS", "4")
    result = run_pipeline(
        str(FIXTURES / "parallel-config.yml"),
        str(FIXTURES / "parallel-target.py"),
        "",
        include_skipped=True,
    )
    rule_order = [
        r.get("rule") or r.get("id")
        for r in result.get("rules_evaluated", [])
        if r.get("engine") == "script"
    ]
    assert len(rule_order) == 4, f"expected 4 script records, got {len(rule_order)}: {rule_order}"
    assert rule_order == ["sleep-a", "sleep-b", "sleep-c", "sleep-d"]
