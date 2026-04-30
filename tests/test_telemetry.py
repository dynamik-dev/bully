"""Tests for telemetry logging (opt-in via .bully/ directory)."""

import json
from pathlib import Path

from bully import run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"


def test_no_log_written_when_directory_missing(tmp_path):
    # Copy config into a tmp project with NO .bully/ dir
    config = tmp_path / ".bully.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    run_pipeline(str(config), str(FIXTURES / "clean.php"), "")
    assert not (tmp_path / ".bully").exists()


def _multiline_diff() -> str:
    # Can't-match filter (plan 4.2) requires >= 2 added lines for semantic rules.
    return (
        "--- a/clean.php\n"
        "+++ b/clean.php\n"
        "@@ -10,2 +10,3 @@\n"
        "+    $result = User::query()->get();\n"
        "+    return ['users' => $result];\n"
    )


def test_log_written_when_directory_exists(tmp_path):
    config = tmp_path / ".bully.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    (tmp_path / ".bully").mkdir()

    run_pipeline(str(config), str(FIXTURES / "clean.php"), _multiline_diff())

    log = tmp_path / ".bully" / "log.jsonl"
    assert log.exists()
    # Filter to the main pipeline record (ignore new semantic_skipped type).
    records = [json.loads(line) for line in log.read_text().strip().splitlines()]
    main = [r for r in records if "rules" in r]
    assert len(main) == 1
    record = main[0]
    assert "ts" in record
    assert "file" in record
    assert record["status"] in ("pass", "evaluate", "blocked")
    assert isinstance(record["rules"], list)


def test_log_records_script_verdicts(tmp_path):
    config = tmp_path / ".bully.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    (tmp_path / ".bully").mkdir()

    run_pipeline(str(config), str(FIXTURES / "violation.php"), "")

    log = tmp_path / ".bully" / "log.jsonl"
    record = json.loads(log.read_text().strip().splitlines()[-1])
    rule_records = {r["id"]: r for r in record["rules"]}
    assert "no-compact" in rule_records
    assert rule_records["no-compact"]["engine"] == "script"
    assert rule_records["no-compact"]["verdict"] == "violation"
    assert record["status"] == "blocked"


def test_log_records_semantic_requested(tmp_path):
    config = tmp_path / ".bully.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    (tmp_path / ".bully").mkdir()

    run_pipeline(str(config), str(FIXTURES / "clean.php"), _multiline_diff())

    log = tmp_path / ".bully" / "log.jsonl"
    records = [json.loads(line) for line in log.read_text().strip().splitlines()]
    main = [r for r in records if "rules" in r][-1]
    rule_records = {r["id"]: r for r in main["rules"]}
    assert "inline-single-use-vars" in rule_records
    assert rule_records["inline-single-use-vars"]["engine"] == "semantic"
    assert rule_records["inline-single-use-vars"]["verdict"] == "evaluate_requested"


def test_log_appends_on_repeat_runs(tmp_path):
    config = tmp_path / ".bully.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    (tmp_path / ".bully").mkdir()

    run_pipeline(str(config), str(FIXTURES / "clean.php"), "")
    run_pipeline(str(config), str(FIXTURES / "clean.php"), "")
    run_pipeline(str(config), str(FIXTURES / "violation.php"), "")

    log = tmp_path / ".bully" / "log.jsonl"
    # Main pipeline records (one per run); auxiliary semantic_skipped records
    # are filtered out since the plan (4.2) explicitly adds them.
    records = [json.loads(line) for line in log.read_text().strip().splitlines()]
    main = [r for r in records if "rules" in r]
    assert len(main) == 3
