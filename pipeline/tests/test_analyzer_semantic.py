"""Tests that the analyzer consumes semantic_verdict and semantic_skipped records."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer import analyze

FIXTURES = Path(__file__).parent / "fixtures"


def _write_log(path: Path, records: list[dict]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_semantic_verdict_violation_counts_as_fire(tmp_path):
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            {
                "ts": "2026-04-16T12:00:00Z",
                "type": "semantic_verdict",
                "rule": "inline-single-use-vars",
                "verdict": "violation",
                "file": "src/F.php",
                "severity": "error",
            }
        ],
    )
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    row = report["by_rule"]["inline-single-use-vars"]
    assert row["fires"] == 1
    assert row["passes"] == 0
    assert "inline-single-use-vars" not in report["dead"]


def test_semantic_verdict_pass_counts_as_pass(tmp_path):
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            {
                "ts": "2026-04-16T12:00:00Z",
                "type": "semantic_verdict",
                "rule": "inline-single-use-vars",
                "verdict": "pass",
                "file": "src/F.php",
                "severity": "error",
            }
        ],
    )
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    row = report["by_rule"]["inline-single-use-vars"]
    assert row["fires"] == 0
    assert row["passes"] == 1
    assert "inline-single-use-vars" not in report["dead"]


def test_semantic_skipped_keeps_rule_alive(tmp_path):
    """Per docs/telemetry.md: a rule skipped only by can't-match filters is alive, not dead."""
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            {
                "ts": "2026-04-16T12:00:00Z",
                "type": "semantic_skipped",
                "rule": "inline-single-use-vars",
                "reason": "whitespace_only",
                "file": "src/F.php",
            }
        ],
    )
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    assert "inline-single-use-vars" not in report["dead"]
    row = report["by_rule"]["inline-single-use-vars"]
    assert row["skipped"] == 1


def test_skip_only_rule_is_not_dead_but_has_zero_invocations(tmp_path):
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            {
                "ts": "2026-04-16T12:00:00Z",
                "type": "semantic_skipped",
                "rule": "inline-single-use-vars",
                "reason": "comment_only",
                "file": "src/F.php",
            }
        ],
    )
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    row = report["by_rule"]["inline-single-use-vars"]
    assert row["fires"] == 0
    assert row["passes"] == 0
    assert row["skipped"] == 1


def test_format_report_includes_skipped_column(tmp_path):
    from analyzer import format_report

    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            {
                "ts": "2026-04-16T12:00:00Z",
                "type": "semantic_skipped",
                "rule": "inline-single-use-vars",
                "reason": "whitespace_only",
                "file": "src/F.php",
            }
        ],
    )
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    text = format_report(report)
    assert "skipped=1" in text
