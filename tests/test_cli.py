"""Tests for pipeline CLI: exit codes, stderr/stdout routing, JSON input."""

import json
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def run_cli(args, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "bully", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_exit_zero_when_no_config():
    r = run_cli(["/nonexistent/config.yml", "some/file.php"])
    assert r.returncode == 0


def test_exit_two_when_blocked():
    r = run_cli([str(FIXTURES / "basic-config.yml"), str(FIXTURES / "violation.php")])
    assert r.returncode == 2
    # Violations must be on stderr per Claude Code hook contract
    assert "no-compact" in r.stderr or "violation" in r.stderr.lower()


def test_exit_zero_on_pass_with_no_matching_rules():
    r = run_cli([str(FIXTURES / "basic-config.yml"), "docs/readme.md"])
    assert r.returncode == 0


def test_exit_zero_on_evaluate_status():
    r = run_cli([str(FIXTURES / "basic-config.yml"), str(FIXTURES / "clean.php")])
    # Has a semantic rule (inline-single-use-vars) → evaluate, not block
    assert r.returncode == 0


def test_json_stdin_input_is_parsed():
    """bully.py should accept JSON on stdin containing tool_name/old/new strings."""
    payload = {
        "tool_name": "Edit",
        "file_path": str(FIXTURES / "clean.php"),
        "old_string": "return ['users' => User::query()->get()];",
        "new_string": "return ['users' => User::query()->get()];",
    }
    r = run_cli(
        [str(FIXTURES / "basic-config.yml"), str(FIXTURES / "clean.php")],
        stdin=json.dumps(payload),
    )
    assert r.returncode == 0  # clean file, no violations


def test_stderr_format_on_block_is_agent_readable():
    r = run_cli([str(FIXTURES / "basic-config.yml"), str(FIXTURES / "violation.php")])
    assert r.returncode == 2
    # Must be human/agent-readable text, not JSON blob
    assert "AGENTIC LINT" in r.stderr
    assert "line" in r.stderr.lower()
