"""Tests for telemetry logging (opt-in via .agentic-lint/ directory)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"


def test_no_log_written_when_directory_missing(tmp_path):
    # Copy config into a tmp project with NO .agentic-lint/ dir
    config = tmp_path / ".agentic-lint.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    run_pipeline(str(config), str(FIXTURES / "clean.php"), "")
    assert not (tmp_path / ".agentic-lint").exists()


def test_log_written_when_directory_exists(tmp_path):
    config = tmp_path / ".agentic-lint.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    (tmp_path / ".agentic-lint").mkdir()

    run_pipeline(str(config), str(FIXTURES / "clean.php"), "+ new line")

    log = tmp_path / ".agentic-lint" / "log.jsonl"
    assert log.exists()
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "ts" in record
    assert "file" in record
    assert record["status"] in ("pass", "evaluate", "blocked")
    assert "rules" in record
    assert isinstance(record["rules"], list)


def test_log_records_script_verdicts(tmp_path):
    config = tmp_path / ".agentic-lint.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    (tmp_path / ".agentic-lint").mkdir()

    run_pipeline(str(config), str(FIXTURES / "violation.php"), "")

    log = tmp_path / ".agentic-lint" / "log.jsonl"
    record = json.loads(log.read_text().strip().splitlines()[-1])
    rule_records = {r["id"]: r for r in record["rules"]}
    assert "no-compact" in rule_records
    assert rule_records["no-compact"]["engine"] == "script"
    assert rule_records["no-compact"]["verdict"] == "violation"
    assert record["status"] == "blocked"


def test_log_records_semantic_requested(tmp_path):
    config = tmp_path / ".agentic-lint.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    (tmp_path / ".agentic-lint").mkdir()

    run_pipeline(str(config), str(FIXTURES / "clean.php"), "")

    log = tmp_path / ".agentic-lint" / "log.jsonl"
    record = json.loads(log.read_text().strip().splitlines()[-1])
    rule_records = {r["id"]: r for r in record["rules"]}
    assert "inline-single-use-vars" in rule_records
    assert rule_records["inline-single-use-vars"]["engine"] == "semantic"
    assert rule_records["inline-single-use-vars"]["verdict"] == "evaluate_requested"


def test_log_appends_on_repeat_runs(tmp_path):
    config = tmp_path / ".agentic-lint.yml"
    config.write_text((FIXTURES / "basic-config.yml").read_text())
    (tmp_path / ".agentic-lint").mkdir()

    run_pipeline(str(config), str(FIXTURES / "clean.php"), "")
    run_pipeline(str(config), str(FIXTURES / "clean.php"), "")
    run_pipeline(str(config), str(FIXTURES / "violation.php"), "")

    log = tmp_path / ".agentic-lint" / "log.jsonl"
    assert len(log.read_text().strip().splitlines()) == 3
