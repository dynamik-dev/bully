"""Tests that the analyzer consumes semantic_verdict and semantic_skipped records."""

import json
from pathlib import Path

from bully.semantic.analyzer import analyze

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
    from bully.semantic.analyzer import format_report

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


def test_mixed_record_types_aggregate_correctly(tmp_path):
    """All three record shapes (semantic_verdict, semantic_skipped, legacy rules array)
    must aggregate into a single by_rule table without double-counting and with
    files_touched deduplicated across record types."""
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            # Rule X = inline-single-use-vars: semantic_verdict violation + array violation -> fires == 2
            {
                "ts": "2026-04-16T12:00:00Z",
                "type": "semantic_verdict",
                "rule": "inline-single-use-vars",
                "verdict": "violation",
                "file": "src/F.php",
                "severity": "error",
            },
            {
                "ts": "2026-04-16T12:00:01Z",
                "file": "src/F.php",
                "rules": [{"id": "inline-single-use-vars", "verdict": "violation"}],
            },
            # Rule Y = no-compact: semantic_skipped + array verdict pass on same file
            # -> skipped == 1 (independent), passes == 1, fires == 0
            {
                "ts": "2026-04-16T12:00:02Z",
                "type": "semantic_skipped",
                "rule": "no-compact",
                "reason": "whitespace_only",
                "file": "src/F.php",
            },
            {
                "ts": "2026-04-16T12:00:03Z",
                "file": "src/F.php",
                "rules": [{"id": "no-compact", "verdict": "pass"}],
            },
        ],
    )
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))

    # Rule X: verdict + array fires aggregate
    x = report["by_rule"]["inline-single-use-vars"]
    assert x["fires"] == 2
    assert x["passes"] == 0
    assert x["skipped"] == 0
    # files_touched deduplicates across record types
    assert x["files_touched"] == 1

    # Rule Y: skip counted independently of verdict
    y = report["by_rule"]["no-compact"]
    assert y["skipped"] == 1
    assert y["fires"] == 0
    assert y["passes"] == 1
    assert y["files_touched"] == 1
