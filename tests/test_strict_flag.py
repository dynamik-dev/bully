"""Tests for `bully lint --strict`.

The default posture is advisory: the PostToolUse hook returns 0 even on
untrusted configs because blocking every edit on infra issues is
user-hostile. CI callers need the opposite -- any non-pass status should
fail loud. `--strict` flips that default for the CLI path only; the
hook path is unaffected.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _run(
    argv: list[str], cwd: Path, env: dict | None = None, stdin: str = ""
) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    # Clear BULLY_TRUST_ALL so trust gating actually runs -- conftest
    # opts into it for the in-process tests, but for subprocess tests we
    # want the real trust flow.
    merged_env.pop("BULLY_TRUST_ALL", None)
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "bully", *argv],
        cwd=str(cwd),
        env=merged_env,
        capture_output=True,
        text=True,
        input=stdin,
    )


def _write_simple_config(tmp_path: Path) -> Path:
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        textwrap.dedent(
            """
            rules:
              noop:
                description: "never fires"
                engine: script
                scope: "*.py"
                severity: error
                script: "exit 0"
            """
        )
    )
    return cfg


def test_strict_exits_nonzero_when_untrusted(tmp_path):
    _write_simple_config(tmp_path)
    target = tmp_path / "src.py"
    target.write_text("x = 1\n")
    r = _run(["lint", str(target), "--strict"], cwd=tmp_path)
    assert r.returncode != 0, f"expected non-zero, stdout={r.stdout!r} stderr={r.stderr!r}"


def test_default_exits_zero_when_untrusted(tmp_path):
    _write_simple_config(tmp_path)
    target = tmp_path / "src.py"
    target.write_text("x = 1\n")
    r = _run(["lint", str(target)], cwd=tmp_path)
    # Default is advisory -- exit 0 even when untrusted, so the PostToolUse
    # hook never blocks edits on infra issues.
    assert r.returncode == 0, f"expected 0, stdout={r.stdout!r} stderr={r.stderr!r}"


def test_strict_exits_zero_on_pass(tmp_path):
    _write_simple_config(tmp_path)
    target = tmp_path / "src.py"
    target.write_text("x = 1\n")
    # Trust the config first, then a clean lint with --strict exits 0.
    _run(["trust", "--config", str(tmp_path / ".bully.yml")], cwd=tmp_path)
    r = _run(["lint", str(target), "--strict"], cwd=tmp_path)
    assert r.returncode == 0, f"expected 0, stdout={r.stdout!r} stderr={r.stderr!r}"


@pytest.mark.skipif(os.name == "nt", reason="shell script rule uses sh")
def test_strict_still_exits_2_on_blocked(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        textwrap.dedent(
            """
            rules:
              always-fails:
                description: "always produces a violation"
                engine: script
                scope: "*.py"
                severity: error
                script: "echo 'src.py:1: something bad'; exit 1"
            """
        )
    )
    target = tmp_path / "src.py"
    target.write_text("x = 1\n")
    _run(["trust", "--config", str(cfg)], cwd=tmp_path)
    r = _run(["lint", str(target), "--strict"], cwd=tmp_path)
    # Blocked keeps its dedicated exit code regardless of --strict; 2 is
    # the agent-contract value that tells Claude Code "this edit must be
    # fixed before proceeding."
    assert r.returncode == 2, f"expected 2, stdout={r.stdout!r} stderr={r.stderr!r}"
