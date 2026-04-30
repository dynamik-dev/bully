"""Tests for --log-verdict telemetry append."""

import json
import subprocess
import sys
from pathlib import Path

RULE_YAML = (
    "rules:\n"
    "  my-rule:\n"
    '    description: "semantic rule"\n'
    "    engine: semantic\n"
    '    scope: "*.py"\n'
    "    severity: error\n"
)


def _run(args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bully", *args],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(cwd) if cwd else None,
    )


def test_log_verdict_appends_record(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    (tmp_path / ".bully").mkdir()

    r = _run(
        [
            "--log-verdict",
            "--config",
            str(tmp_path / ".bully.yml"),
            "--rule",
            "my-rule",
            "--verdict",
            "violation",
            "--file",
            "src/app.py",
        ]
    )
    assert r.returncode == 0, f"stderr={r.stderr}"

    log = tmp_path / ".bully" / "log.jsonl"
    assert log.is_file()
    records = [json.loads(line) for line in log.read_text().strip().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["type"] == "semantic_verdict"
    assert record["rule"] == "my-rule"
    assert record["verdict"] == "violation"
    assert record["file"] == "src/app.py"
    assert "ts" in record


def test_log_verdict_pass_recorded(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    (tmp_path / ".bully").mkdir()

    r = _run(
        [
            "--log-verdict",
            "--config",
            str(tmp_path / ".bully.yml"),
            "--rule",
            "my-rule",
            "--verdict",
            "pass",
        ]
    )
    assert r.returncode == 0

    records = [
        json.loads(line)
        for line in (tmp_path / ".bully" / "log.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert records[0]["verdict"] == "pass"
    # file is optional and should be absent
    assert "file" not in records[0]


def test_log_verdict_silently_skips_when_no_dir(tmp_path):
    # No .bully/ directory -> telemetry disabled; exit 0 without error.
    (tmp_path / ".bully.yml").write_text(RULE_YAML)

    r = _run(
        [
            "--log-verdict",
            "--config",
            str(tmp_path / ".bully.yml"),
            "--rule",
            "my-rule",
            "--verdict",
            "pass",
        ]
    )
    assert r.returncode == 0
    # No log file was created
    assert not (tmp_path / ".bully" / "log.jsonl").exists()
    assert "telemetry disabled" in r.stderr.lower()


def test_log_verdict_requires_rule_and_verdict(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    r = _run(["--log-verdict", "--config", str(tmp_path / ".bully.yml")])
    assert r.returncode != 0
    assert "usage" in r.stderr.lower() or "rule" in r.stderr.lower()


def test_log_verdict_invalid_verdict_rejected(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    r = _run(
        [
            "--log-verdict",
            "--config",
            str(tmp_path / ".bully.yml"),
            "--rule",
            "my-rule",
            "--verdict",
            "sometimes",
        ]
    )
    # argparse choices enforcement -> exit 2
    assert r.returncode != 0
    assert "invalid choice" in r.stderr.lower() or "sometimes" in r.stderr.lower()


def test_log_verdict_appends_without_overwriting(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    (tmp_path / ".bully").mkdir()
    # Pre-existing log line
    log = tmp_path / ".bully" / "log.jsonl"
    log.write_text('{"existing":"entry"}\n')

    _run(
        [
            "--log-verdict",
            "--config",
            str(tmp_path / ".bully.yml"),
            "--rule",
            "my-rule",
            "--verdict",
            "pass",
        ]
    )
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["existing"] == "entry"
    assert json.loads(lines[1])["type"] == "semantic_verdict"
