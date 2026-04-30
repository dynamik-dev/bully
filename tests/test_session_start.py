"""Tests for the SessionStart-driven banner output."""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bully", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_session_start_prints_rule_count(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  a:
    description: A
    severity: error
    engine: script
    scope: ['**']
    script: 'true'
  b:
    description: B
    severity: warning
    engine: script
    scope: ['**']
    script: 'true'
"""
    )
    p = _run(["session-start"], tmp_path)
    assert p.returncode == 0
    assert "bully active" in p.stdout
    assert "2 rules" in p.stdout
    assert "bully guide" in p.stdout


def test_session_start_with_no_config_is_silent(tmp_path):
    p = _run(["session-start"], tmp_path)
    assert p.returncode == 0
    assert p.stdout == ""


def test_session_start_writes_session_init_telemetry_record(tmp_path):
    """Session start stamps the producer version + schema version into telemetry.

    Lets the analyzer (and forensic readers of old logs) attribute later
    records back to the specific bully release that produced them. Without
    this, a record's shape would be the only signal — and shape changes
    subtly between versions.
    """
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  a:
    description: A
    severity: error
    engine: script
    scope: ['**']
    script: 'true'
"""
    )
    (tmp_path / ".bully").mkdir()
    p = _run(["session-start"], tmp_path)
    assert p.returncode == 0
    log = tmp_path / ".bully" / "log.jsonl"
    assert log.exists()
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    init = next((r for r in records if r.get("type") == "session_init"), None)
    assert init is not None, records
    assert isinstance(init.get("bully_version"), str)
    assert init["bully_version"]  # not empty
    assert init.get("schema_version") == 1
    assert "ts" in init


def test_session_start_skips_telemetry_when_dotbully_missing(tmp_path):
    """Telemetry is opt-in: no .bully/ directory means no init record written."""
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  a:
    description: A
    severity: error
    engine: script
    scope: ['**']
    script: 'true'
"""
    )
    # Note: no .bully/ directory created.
    p = _run(["session-start"], tmp_path)
    assert p.returncode == 0
    assert not (tmp_path / ".bully" / "log.jsonl").exists()
