"""Tests for `bully` subcommand-style invocation.

The console script entrypoint declared in pyproject.toml maps `bully` to
`bully:main`, so `bully validate` is just `python bully.py
validate`. These tests exercise the subcommand normalization layer that
translates `validate`/`doctor`/`lint <file>`/`show-resolved-config` into the
existing flag-based interface, while keeping the legacy positional form (used
by `hook.sh`) and the explicit `--validate`/`--doctor` flags working.
"""

import json
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _run(args, cwd: Path | None = None, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bully", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(cwd) if cwd else None,
    )


# ---- validate subcommand ----


def test_validate_subcommand_clean_config():
    r = _run(["validate", "--config", str(FIXTURES / "basic-config.yml")])
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "[OK]" in r.stdout


def test_validate_subcommand_equals_flag_form():
    sub = _run(["validate", "--config", str(FIXTURES / "basic-config.yml")])
    flag = _run(["--validate", "--config", str(FIXTURES / "basic-config.yml")])
    assert sub.returncode == flag.returncode == 0
    assert sub.stdout == flag.stdout


def test_validate_subcommand_uses_default_config(tmp_path):
    r = _run(["validate"], cwd=tmp_path)
    assert r.returncode == 1
    assert ".bully.yml" in r.stderr


# ---- doctor subcommand ----


def test_doctor_subcommand_runs(tmp_path):
    r = _run(["doctor"], cwd=tmp_path)
    # Doctor exits 1 in a directory with no .bully.yml -- we just want the
    # subcommand to dispatch to _cmd_doctor (proven by the [FAIL] line).
    assert "Python" in r.stdout  # First doctor line is the python-version OK line
    assert "no .bully.yml" in r.stdout


# ---- show-resolved-config subcommand ----


def test_show_resolved_config_subcommand():
    r = _run(["show-resolved-config", "--config", str(FIXTURES / "basic-config.yml")])
    assert r.returncode == 0
    assert "engine=" in r.stdout
    assert "scope=" in r.stdout


# ---- lint subcommand ----


def test_lint_subcommand_with_explicit_config():
    r = _run(
        [
            "lint",
            str(FIXTURES / "violation.php"),
            "--config",
            str(FIXTURES / "basic-config.yml"),
        ]
    )
    # violation.php has a script-rule violation
    assert r.returncode == 2


def test_lint_subcommand_defaults_config_to_dot_bully_yml(tmp_path):
    # Drop a config and a target file in tmp_path; run `bully.py lint <file>`
    # from that directory. Should pick up ./.bully.yml automatically.
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "schema_version: 1\n"
        "rules:\n"
        "  banned-keyword:\n"
        '    description: "no banned-token in source"\n'
        "    engine: script\n"
        '    scope: ["*.py"]\n'
        "    severity: error\n"
        '    script: "grep -nE banned-token {file} && exit 1 || exit 0"\n'
    )
    target = tmp_path / "x.py"
    target.write_text("print('banned-token here')\n")

    # Trust the config so the trust gate doesn't short-circuit.
    trust_env = {"BULLY_TRUST_ALL": "1"}
    r = subprocess.run(
        [sys.executable, "-m", "bully", "lint", str(target)],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(tmp_path),
        env={**__import__("os").environ, **trust_env},
    )
    assert r.returncode == 2, f"stdout={r.stdout}\nstderr={r.stderr}"


def test_lint_subcommand_passes_through_rule_filter():
    diff = (
        "--- a/violation.php\n+++ b/violation.php\n"
        "@@ -10,2 +10,3 @@\n"
        "+    $x = User::query()->get();\n"
        "+    return $x;\n"
    )
    r = _run(
        [
            "lint",
            str(FIXTURES / "violation.php"),
            "--config",
            str(FIXTURES / "basic-config.yml"),
            "--rule",
            "inline-single-use-vars",
            "--diff",
            diff,
        ]
    )
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["status"] == "evaluate"


# ---- legacy paths preserved ----


def test_legacy_flag_form_still_works():
    r = _run(["--validate", "--config", str(FIXTURES / "basic-config.yml")])
    assert r.returncode == 0


def test_legacy_positional_form_still_works():
    r = _run([str(FIXTURES / "basic-config.yml"), str(FIXTURES / "violation.php")])
    assert r.returncode == 2


def test_unknown_first_token_treated_as_positional_config():
    # Ensures we don't accidentally swallow paths that look like subcommand verbs.
    # `nonexistent.yml` is a bare filename and must be passed through to the legacy
    # positional handling, which then fails with the no-config-found pass.
    r = _run(["/nonexistent/config.yml", "some/file.php"])
    assert r.returncode == 0  # pipeline returns pass when config missing
