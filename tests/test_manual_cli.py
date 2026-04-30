"""Tests for manual CLI flags: --rule filter, --print-prompt, flag form."""

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


def test_flag_form_accepts_config_and_file():
    r = run_cli(
        [
            "--config",
            str(FIXTURES / "basic-config.yml"),
            "--file",
            str(FIXTURES / "violation.php"),
        ]
    )
    assert r.returncode == 2


def test_rule_filter_runs_only_matching_rule():
    # basic-config.yml has no-compact (script) and inline-single-use-vars (semantic).
    # Filter to just the semantic rule -- should skip script, produce evaluate status.
    # Plan 4.2 can't-match filter requires >= 2 added diff lines to dispatch semantic.
    diff = (
        "--- a/violation.php\n+++ b/violation.php\n"
        "@@ -10,2 +10,3 @@\n"
        "+    $x = User::query()->get();\n"
        "+    return $x;\n"
    )
    r = run_cli(
        [
            "--config",
            str(FIXTURES / "basic-config.yml"),
            "--file",
            str(FIXTURES / "violation.php"),
            "--rule",
            "inline-single-use-vars",
            "--diff",
            diff,
        ]
    )
    # Semantic-only -> exit 0, evaluate payload on stdout
    assert r.returncode == 0
    assert "inline-single-use-vars" in r.stdout
    assert "no-compact" not in r.stdout


def test_rule_filter_nonexistent_rule():
    r = run_cli(
        [
            "--config",
            str(FIXTURES / "basic-config.yml"),
            "--file",
            str(FIXTURES / "violation.php"),
            "--rule",
            "does-not-exist",
        ]
    )
    # No matching rule → pass
    assert r.returncode == 0
    assert '"status"' in r.stdout
    assert "pass" in r.stdout


def test_print_prompt_outputs_semantic_prompt():
    diff = (
        "--- a/clean.php\n+++ b/clean.php\n"
        "@@ -10,2 +10,3 @@\n"
        "+    $x = User::query()->get();\n"
        "+    return $x;\n"
    )
    r = run_cli(
        [
            "--config",
            str(FIXTURES / "basic-config.yml"),
            "--file",
            str(FIXTURES / "clean.php"),
            "--print-prompt",
            "--diff",
            diff,
        ]
    )
    assert r.returncode == 0
    # The prompt text should mention the rule and the file
    assert "inline-single-use-vars" in r.stdout
    assert "clean.php" in r.stdout


def test_positional_form_still_works_for_hook_compat():
    r = run_cli(
        [
            str(FIXTURES / "basic-config.yml"),
            str(FIXTURES / "violation.php"),
        ]
    )
    assert r.returncode == 2
