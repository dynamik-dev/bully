"""Tests for `bully debt` -- baseline + per-line disable governance."""

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


def test_debt_lists_per_line_disables(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text("rules: {}\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.php").write_text(
        "<?php\n"
        "// bully-disable-line no-compact reason: legacy api shape\n"
        "compact('a', 'b');\n"
        "// bully-disable-line no-event reason: x\n"
        "event('user.login');\n"
    )
    p = _run(["debt"], tmp_path)
    assert p.returncode == 0, p.stderr
    assert "no-compact" in p.stdout
    assert "no-event" in p.stdout
    assert "src/foo.php" in p.stdout


def test_debt_flags_short_reasons(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text("rules: {}\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "bad.php").write_text(
        "<?php\n// bully-disable-line no-compact reason: x\ncompact('a');\n"
    )
    p = _run(["debt", "--strict"], tmp_path)
    assert p.returncode != 0
    assert "reason too short" in p.stdout.lower() or "reason too short" in p.stderr.lower()
