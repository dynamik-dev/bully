"""Tests for session-scope rules and the Stop hook driver."""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "pipeline"))

from bully import parse_config  # noqa: E402


def _run(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bully", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_session(bully_dir: Path, files: list[str]) -> None:
    bully_dir.mkdir(exist_ok=True)
    lines = "".join(json.dumps({"file": f}) + "\n" for f in files)
    (bully_dir / "session.jsonl").write_text(lines)


def test_session_engine_rule_parses(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  auth-needs-tests:
    description: Auth changed without tests
    severity: error
    engine: session
    when:
      changed_any: ['src/auth/**']
    require:
      changed_any: ['tests/**/*auth*']
"""
    )
    rules = parse_config(str(cfg))
    rule = next(r for r in rules if r.id == "auth-needs-tests")
    assert rule.engine == "session"
    assert rule.when == {"changed_any": ["src/auth/**"]}
    assert rule.require == {"changed_any": ["tests/**/*auth*"]}


def test_stop_blocks_when_required_files_absent(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  auth-needs-tests:
    description: Auth changed without tests
    severity: error
    engine: session
    when:
      changed_any: ['src/auth/**']
    require:
      changed_any: ['tests/**/*auth*']
"""
    )
    _write_session(tmp_path / ".bully", ["src/auth/login.py"])
    p = _run(["stop"], tmp_path)
    assert p.returncode == 2, (p.stdout, p.stderr)
    assert "auth-needs-tests" in p.stderr


def test_stop_passes_when_required_files_present(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  auth-needs-tests:
    description: Auth changed without tests
    severity: error
    engine: session
    when:
      changed_any: ['src/auth/**']
    require:
      changed_any: ['tests/**/*auth*']
"""
    )
    _write_session(tmp_path / ".bully", ["src/auth/login.py", "tests/test_auth_login.py"])
    p = _run(["stop"], tmp_path)
    assert p.returncode == 0


def test_stop_no_session_file_passes(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  any-rule:
    description: x
    severity: error
    engine: session
    when:
      changed_any: ['**']
    require:
      changed_any: ['tests/**']
"""
    )
    p = _run(["stop"], tmp_path)
    assert p.returncode == 0


def test_session_record_appends_changed_path(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text("rules: {}\n")
    p = _run(["session-record", "--file", "src/foo.py"], tmp_path)
    assert p.returncode == 0
    lines = (tmp_path / ".bully" / "session.jsonl").read_text().strip().splitlines()
    recorded = [json.loads(line)["file"] for line in lines]
    assert "src/foo.py" in recorded


def test_stop_warning_severity_returns_zero_but_prints(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  auth-tests-warning:
    description: Auth changed without tests (warning only)
    severity: warning
    engine: session
    when:
      changed_any: ['src/auth/**']
    require:
      changed_any: ['tests/**/*auth*']
"""
    )
    _write_session(tmp_path / ".bully", ["src/auth/login.py"])
    p = _run(["stop"], tmp_path)
    assert p.returncode == 0, (p.stdout, p.stderr)
    # Warning still surfaces in stderr (visible to the user) but doesn't block.
    assert "auth-tests-warning" in p.stderr


def test_stop_warning_only_resets_session_file(tmp_path):
    """Warning-only stop must delete session.jsonl so warnings don't re-fire.

    Without the reset, a session rule that legitimately doesn't apply this
    session would emit the same stale warning on every subsequent Stop until
    a clean stop occurred -- the bug Codex 5.5 flagged.
    """
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  auth-tests-warning:
    description: Auth changed without tests (warning only)
    severity: warning
    engine: session
    when:
      changed_any: ['src/auth/**']
    require:
      changed_any: ['tests/**/*auth*']
"""
    )
    bully_dir = tmp_path / ".bully"
    session_file = bully_dir / "session.jsonl"
    _write_session(bully_dir, ["src/auth/login.py"])
    assert session_file.exists()

    p = _run(["stop"], tmp_path)

    assert p.returncode == 0, (p.stdout, p.stderr)
    # Warning text reaches stderr.
    assert "auth-tests-warning" in p.stderr
    assert "session check failed" in p.stderr
    # And the session file was reset so the next session starts fresh.
    assert not session_file.exists()


def test_stop_error_severity_preserves_session_file(tmp_path):
    """Error-severity stop must NOT delete session.jsonl.

    The user is forced to clear the violations deliberately (by satisfying
    the rule, or by removing the offending changes) before the session
    state resets. Otherwise blocking errors could be silently swallowed
    on the next Stop.
    """
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  auth-needs-tests:
    description: Auth changed without tests
    severity: error
    engine: session
    when:
      changed_any: ['src/auth/**']
    require:
      changed_any: ['tests/**/*auth*']
"""
    )
    bully_dir = tmp_path / ".bully"
    session_file = bully_dir / "session.jsonl"
    _write_session(bully_dir, ["src/auth/login.py"])
    assert session_file.exists()

    p = _run(["stop"], tmp_path)

    assert p.returncode == 2, (p.stdout, p.stderr)
    # Error text reaches stderr.
    assert "auth-needs-tests" in p.stderr
    assert "session check failed" in p.stderr
    # And the session file is preserved so the violation persists until
    # the user resolves it deliberately.
    assert session_file.exists()
    assert "src/auth/login.py" in session_file.read_text()


def test_stop_clean_resets_session_file(tmp_path):
    """Sanity: a clean (no-violation) stop also deletes session.jsonl.

    This is the original behavior preserved by the warning-only fix.
    """
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  auth-needs-tests:
    description: Auth changed without tests
    severity: error
    engine: session
    when:
      changed_any: ['src/auth/**']
    require:
      changed_any: ['tests/**/*auth*']
"""
    )
    bully_dir = tmp_path / ".bully"
    session_file = bully_dir / "session.jsonl"
    # Both the trigger file AND the required tests file are present, so the
    # rule is satisfied and no violation fires.
    _write_session(bully_dir, ["src/auth/login.py", "tests/test_auth_login.py"])
    assert session_file.exists()

    p = _run(["stop"], tmp_path)

    assert p.returncode == 0, (p.stdout, p.stderr)
    assert not session_file.exists()
