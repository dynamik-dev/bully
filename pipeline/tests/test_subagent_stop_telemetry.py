"""SubagentStop appends a `subagent_stop` telemetry record."""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = REPO_ROOT / "pipeline" / "pipeline.py"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PIPELINE), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_subagent_stop_writes_record(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text("rules: {}\n")
    (tmp_path / ".bully").mkdir()
    p = _run(["subagent-stop"], tmp_path)
    assert p.returncode == 0
    log = tmp_path / ".bully" / "log.jsonl"
    assert log.exists()
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    types = [r.get("type") for r in records]
    assert "subagent_stop" in types
