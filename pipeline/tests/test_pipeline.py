"""Tests for semantic payload builder and full pipeline orchestration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import Rule, build_semantic_payload, run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"


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
    payload = build_semantic_payload("test.php", "+ new line", ["no-compact"], rules)
    assert payload["file"] == "test.php"
    assert payload["diff"] == "+ new line"


def test_payload_includes_passed_checks():
    rules = [
        Rule(id="test-rule", description="Test", engine="semantic", scope="*", severity="error"),
    ]
    payload = build_semantic_payload("test.php", "diff", ["no-compact", "no-db-facade"], rules)
    assert payload["passed_checks"] == ["no-compact", "no-db-facade"]


def test_payload_includes_semantic_rules():
    rules = [
        Rule(id="rule-a", description="First rule", engine="semantic", scope="*", severity="error"),
        Rule(
            id="rule-b", description="Second rule", engine="semantic", scope="*", severity="warning"
        ),
    ]
    payload = build_semantic_payload("test.php", "diff", [], rules)
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
    result = run_pipeline(
        str(FIXTURES / "basic-config.yml"),
        str(FIXTURES / "clean.php"),
        "+ $result = User::query()->get();",
    )
    assert result["status"] == "evaluate"
    assert "no-compact" in result["passed_checks"]
    assert len(result["evaluate"]) >= 1


def test_pipeline_script_block_skips_semantic():
    result = run_pipeline(
        str(FIXTURES / "basic-config.yml"),
        str(FIXTURES / "violation.php"),
        "",
    )
    assert result["status"] == "blocked"
    assert "evaluate" not in result
