"""Tests for the `bully coverage` rule-scope metric."""

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


def test_coverage_reports_per_file_rule_count(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  php-only:
    description: x
    severity: error
    engine: script
    scope: ['**/*.php']
    script: 'true'
  any-file:
    description: y
    severity: warning
    engine: script
    scope: ['**']
    script: 'true'
"""
    )
    (tmp_path / ".bully").mkdir()
    log = tmp_path / ".bully" / "log.jsonl"
    log.write_text(
        json.dumps(
            {
                "ts": "2026-04-16T12:00:00Z",
                "file": "src/foo.php",
                "status": "pass",
                "latency_ms": 5,
                "rules": [
                    {
                        "id": "php-only",
                        "engine": "script",
                        "verdict": "pass",
                        "severity": "error",
                        "latency_ms": 1,
                    }
                ],
            }
        )
        + "\n"
    )
    p = _run(["coverage", "--json"], tmp_path)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert "files" in data
    assert "src/foo.php" in data["files"]
    assert data["files"]["src/foo.php"]["rules_in_scope"] >= 2  # php-only + any-file


def test_coverage_text_output_lists_uncovered_files(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  php-only:
    description: x
    severity: error
    engine: script
    scope: ['**/*.php']
    script: 'true'
"""
    )
    (tmp_path / ".bully").mkdir()
    log = tmp_path / ".bully" / "log.jsonl"
    log.write_text(
        json.dumps(
            {
                "ts": "2026-04-16T12:00:00Z",
                "file": "src/foo.ts",
                "status": "pass",
                "latency_ms": 5,
                "rules": [],
            }
        )
        + "\n"
    )
    p = _run(["coverage"], tmp_path)
    assert p.returncode == 0
    assert "src/foo.ts" in p.stdout
    assert "Uncovered files" in p.stdout
