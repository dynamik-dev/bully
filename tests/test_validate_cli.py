"""Tests for the --validate subcommand."""

import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _run(args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bully", *args],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(cwd) if cwd else None,
    )


def test_validate_clean_config_returns_zero():
    r = _run(["--validate", "--config", str(FIXTURES / "basic-config.yml")])
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "[OK]" in r.stdout
    assert "rule" in r.stdout.lower()


def test_validate_malformed_config_returns_one(tmp_path):
    bad = tmp_path / ".bully.yml"
    bad.write_text(
        "rules:\n"
        "\tbad-tabs:\n"
        '    description: "x"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
    )
    r = _run(["--validate", "--config", str(bad)])
    assert r.returncode == 1
    # ConfigError text should be on stderr
    assert "tab" in r.stderr.lower() or "line" in r.stderr.lower()


def test_validate_unknown_field_fails(tmp_path):
    bad = tmp_path / ".bully.yml"
    bad.write_text(
        "rules:\n"
        "  r1:\n"
        '    description: "x"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
        "    frobnicate: nope\n"
    )
    r = _run(["--validate", "--config", str(bad)])
    assert r.returncode == 1
    assert "frobnicate" in r.stderr


def test_validate_missing_file_fails(tmp_path):
    r = _run(["--validate", "--config", str(tmp_path / "does-not-exist.yml")])
    assert r.returncode == 1
    assert "not found" in r.stderr


def test_validate_default_config_path_when_absent(tmp_path):
    # No config, no --config flag: defaults to ./.bully.yml
    r = _run(["--validate"], cwd=tmp_path)
    assert r.returncode == 1
    assert ".bully.yml" in r.stderr


def test_validate_invalid_severity_fails(tmp_path):
    bad = tmp_path / ".bully.yml"
    bad.write_text(
        "rules:\n"
        "  r1:\n"
        '    description: "x"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: wild-severity\n"
        '    script: "exit 0"\n'
    )
    r = _run(["--validate", "--config", str(bad)])
    assert r.returncode == 1
    assert "severity" in r.stderr.lower()


# ---------------------------------------------------------------------------
# --execute-dry-run: verify each script rule runs against empty input without
# shell/regex-level errors. Catches bad grep patterns at config time.
# ---------------------------------------------------------------------------


def test_execute_dry_run_clean_script_rule_reports_ok(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  clean:\n"
        '    description: "no foobar"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "grep -nE foobar {file} && exit 1 || exit 0"\n'
    )
    r = _run(["--validate", "--config", str(cfg), "--execute-dry-run"])
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "clean" in r.stdout
    assert "[OK]" in r.stdout


def test_execute_dry_run_flags_broken_grep_regex(tmp_path):
    """A grep -E with unbalanced parens must surface as a dry-run failure."""
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  broken-regex:\n"
        '    description: "bad pattern"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        "    script: \"grep -nE '(unclosed' {file} && exit 1 || exit 0\"\n"
    )
    r = _run(["--validate", "--config", str(cfg), "--execute-dry-run"])
    assert r.returncode == 1, f"stdout={r.stdout} stderr={r.stderr}"
    assert "broken-regex" in r.stdout or "broken-regex" in r.stderr


def test_execute_dry_run_flags_nonexistent_command(tmp_path):
    """A script referencing a command that doesn't exist should fail the dry run."""
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  missing-cmd:\n"
        '    description: "missing binary"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "thiscommanddoesnotexist123 {file}"\n'
    )
    r = _run(["--validate", "--config", str(cfg), "--execute-dry-run"])
    assert r.returncode == 1
    assert "missing-cmd" in r.stdout or "missing-cmd" in r.stderr


def test_execute_dry_run_skips_non_script_rules(tmp_path):
    """Semantic and ast rules aren't shell-executed, so dry run skips them."""
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  sem:\n"
        '    description: "semantic only"\n'
        "    engine: semantic\n"
        '    scope: "*"\n'
        "    severity: warning\n"
    )
    r = _run(["--validate", "--config", str(cfg), "--execute-dry-run"])
    assert r.returncode == 0, f"stderr={r.stderr}"
    # Non-script rule is parsed but not dry-run executed.
    assert "sem" in r.stdout


def test_execute_dry_run_requires_validate(tmp_path):
    """--execute-dry-run is only meaningful alongside --validate."""
    r = _run(["--execute-dry-run"])
    assert r.returncode != 0


def test_execute_dry_run_multiple_rules_all_clean(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  a:\n"
        '    description: "a"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
        "  b:\n"
        '    description: "b"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "grep -nE ok {file} && exit 1 || exit 0"\n'
    )
    r = _run(["--validate", "--config", str(cfg), "--execute-dry-run"])
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "a" in r.stdout
    assert "b" in r.stdout
