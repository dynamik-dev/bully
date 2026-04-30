"""Tests for the rule-health analyzer that reads telemetry logs."""

import json
from pathlib import Path

from bully.semantic.analyzer import _read_log, analyze, format_report

FIXTURES = Path(__file__).parent / "fixtures"


def _write_log(path: Path, records: list[dict]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_empty_log_produces_empty_report(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text("")
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    assert report["total_edits"] == 0
    # All configured rules appear with zero counts and are classified as dead.
    assert set(report["dead"]) == {"no-compact", "inline-single-use-vars"}
    assert all(row["invocations"] == 0 for row in report["by_rule"].values())


def test_dead_rules_are_identified(tmp_path):
    log = tmp_path / "log.jsonl"
    # Log mentions only one rule; the config has 2.
    _write_log(
        log,
        [
            {
                "ts": "2026-04-16T12:00:00Z",
                "file": "f.php",
                "status": "pass",
                "latency_ms": 5,
                "rules": [
                    {
                        "id": "no-compact",
                        "engine": "script",
                        "verdict": "pass",
                        "severity": "error",
                        "latency_ms": 3,
                    }
                ],
            }
        ],
    )
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    assert "inline-single-use-vars" in report["dead"]
    assert "no-compact" not in report["dead"]


def test_noisy_rules_identified(tmp_path):
    log = tmp_path / "log.jsonl"
    # no-compact fails 4 of 5 edits -> noisy
    records = []
    for i in range(5):
        verdict = "violation" if i < 4 else "pass"
        records.append(
            {
                "ts": f"2026-04-16T12:0{i}:00Z",
                "file": "f.php",
                "status": "blocked" if verdict == "violation" else "pass",
                "latency_ms": 5,
                "rules": [
                    {
                        "id": "no-compact",
                        "engine": "script",
                        "verdict": verdict,
                        "severity": "error",
                        "latency_ms": 3,
                    }
                ],
            }
        )
    _write_log(log, records)
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"), noisy_threshold=0.3)
    assert "no-compact" in report["noisy"]


def test_slow_rules_identified(tmp_path):
    log = tmp_path / "log.jsonl"
    records = [
        {
            "ts": "2026-04-16T12:00:00Z",
            "file": "f.php",
            "status": "pass",
            "latency_ms": 2000,
            "rules": [
                {
                    "id": "no-compact",
                    "engine": "script",
                    "verdict": "pass",
                    "severity": "error",
                    "latency_ms": 1500,
                }
            ],
        }
        for _ in range(5)
    ]
    _write_log(log, records)
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"), slow_threshold_ms=1000)
    assert "no-compact" in report["slow"]


def test_by_rule_aggregates_counts(tmp_path):
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            {
                "ts": "2026-04-16T12:00:00Z",
                "file": "f.php",
                "status": "blocked",
                "latency_ms": 5,
                "rules": [
                    {
                        "id": "no-compact",
                        "engine": "script",
                        "verdict": "violation",
                        "severity": "error",
                        "latency_ms": 3,
                    },
                    {
                        "id": "no-compact",  # same rule, second file pretend
                        "engine": "script",
                        "verdict": "pass",
                        "severity": "error",
                        "latency_ms": 3,
                    },
                ],
            }
        ],
    )
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    assert report["by_rule"]["no-compact"]["fires"] == 1
    assert report["by_rule"]["no-compact"]["passes"] == 1


def test_format_report_produces_readable_text(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text("")
    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    text = format_report(report)
    assert "Rule health" in text or "rule health" in text.lower()


def test_read_log_skips_corrupt_lines(tmp_path):
    """`_read_log` must survive corrupt JSONL: truncated, garbage, and
    non-dict JSON values should all be skipped without aborting the read."""
    log = tmp_path / "log.jsonl"
    valid_a = {
        "ts": "2026-04-16T12:00:00Z",
        "file": "f.php",
        "status": "pass",
        "latency_ms": 5,
        "rules": [
            {
                "id": "no-compact",
                "engine": "script",
                "verdict": "pass",
                "severity": "error",
                "latency_ms": 3,
            }
        ],
    }
    valid_b = {
        "ts": "2026-04-16T12:01:00Z",
        "file": "g.php",
        "status": "blocked",
        "latency_ms": 7,
        "rules": [
            {
                "id": "no-compact",
                "engine": "script",
                "verdict": "violation",
                "severity": "error",
                "latency_ms": 4,
            }
        ],
    }
    # Mix: valid, truncated (no closing brace), pure garbage, non-dict JSON
    # (a stray scalar, a stray array, a literal null), then valid again.
    with open(log, "w") as f:
        f.write(json.dumps(valid_a) + "\n")
        f.write('{"ts": "2026-04-16T12:00:30Z", "file": "broken.php"\n')  # truncated
        f.write("not json at all !!!\n")  # garbage
        f.write("42\n")  # decodes but not a dict
        f.write("null\n")  # decodes but not a dict
        f.write("[1, 2, 3]\n")  # decodes but not a dict
        f.write(json.dumps(valid_b) + "\n")

    records = _read_log(str(log))
    assert len(records) == 2
    assert records[0] == valid_a
    assert records[1] == valid_b


def test_analyze_survives_corrupt_log(tmp_path):
    """`analyze()` must produce a sane report even when the log contains
    corrupt lines -- the valid records should still be aggregated."""
    log = tmp_path / "log.jsonl"
    rec = {
        "ts": "2026-04-16T12:00:00Z",
        "file": "f.php",
        "status": "blocked",
        "latency_ms": 5,
        "rules": [
            {
                "id": "no-compact",
                "engine": "script",
                "verdict": "violation",
                "severity": "error",
                "latency_ms": 3,
            }
        ],
    }
    with open(log, "w") as f:
        f.write(json.dumps(rec) + "\n")
        f.write('{"partial": "trun\n')  # truncated -- JSONDecodeError
        f.write("###garbage###\n")  # garbage -- JSONDecodeError
        f.write("null\n")  # non-dict JSON
        f.write(json.dumps(rec) + "\n")

    report = analyze(str(log), str(FIXTURES / "basic-config.yml"))
    # Two valid records survived; the corrupt three were skipped.
    assert report["total_edits"] == 2
    assert report["by_rule"]["no-compact"]["fires"] == 2
    # The other configured rule had no traffic -> still classified as dead.
    assert "inline-single-use-vars" in report["dead"]
