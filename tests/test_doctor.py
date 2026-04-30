"""Tests for --doctor diagnostic output."""

import json
import subprocess
import sys
from pathlib import Path

from bully import _check_python_version  # noqa: E402


def test_check_python_version_passes_on_310_and_above():
    ok, msg = _check_python_version((3, 10))
    assert ok is True
    assert msg == "[OK] Python 3.10"

    ok, msg = _check_python_version((3, 12))
    assert ok is True
    assert msg == "[OK] Python 3.12"

    ok, msg = _check_python_version((4, 0))
    assert ok is True
    assert msg == "[OK] Python 4.0"


def test_check_python_version_fails_below_310():
    ok, msg = _check_python_version((3, 9))
    assert ok is False
    assert msg.startswith("[FAIL] Python 3.9 < 3.10")

    ok, msg = _check_python_version((3, 8))
    assert ok is False
    assert "3.8" in msg

    ok, msg = _check_python_version((2, 7))
    assert ok is False
    assert "2.7" in msg


def test_doctor_finds_skills_and_agent_in_plugin_cache(tmp_path):
    """Plugin-installed skills and the evaluator agent live under
    ~/.claude/plugins/cache/<marketplace>/bully/<version>/{skills,agents}/...
    Doctor must accept either the legacy or the plugin path.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / ".bully.yml").write_text(
        "rules:\n"
        "  r1:\n"
        '    description: "d"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
    )
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"command": "hook.sh"}]}]}})
    )

    # Plugin-only layout (no legacy ~/.claude/skills or ~/.claude/agents).
    home = tmp_path / "home"
    plugin_root = home / ".claude" / "plugins" / "cache" / "bully-marketplace" / "bully" / "0.2.0"
    skills_root = plugin_root / "skills"
    for name in ("bully", "bully-init", "bully-author", "bully-review"):
        (skills_root / name).mkdir(parents=True)
        (skills_root / name / "SKILL.md").write_text("# skill\n")
    agents_root = plugin_root / "agents"
    agents_root.mkdir(parents=True)
    (agents_root / "bully-evaluator.md").write_text("# eval\n")

    import os

    env = os.environ.copy()
    env.update({"HOME": str(home), "CLAUDE_HOME": str(home / ".claude")})
    r = subprocess.run(
        [sys.executable, "-m", "bully", "--doctor"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(project),
        env=env,
    )
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "[OK] evaluator agent at" in r.stdout
    assert "(plugin install)" in r.stdout
    for name in ("bully", "bully-init", "bully-author", "bully-review"):
        assert f"[OK] skill {name} present" in r.stdout


def _run_doctor(cwd: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "bully", "--doctor"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(cwd),
        env=env,
    )


def test_doctor_all_pass(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".bully.yml").write_text(
        "rules:\n"
        "  r1:\n"
        '    description: "desc"\n'
        "    engine: script\n"
        '    scope: "*.py"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
    )

    # Per-project settings contain the hook
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"command": "/path/to/hook.sh"}]}]}})
    )

    # Fake HOME with required skills + agent
    home = tmp_path / "home"
    home.mkdir()
    skills = home / ".claude" / "skills"
    for suffix in (
        "bully",
        "bully-init",
        "bully-author",
        "bully-review",
    ):
        (skills / suffix).mkdir(parents=True)
        (skills / suffix / "SKILL.md").write_text("# skill\n")
    agents = home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "bully-evaluator.md").write_text("# eval\n")

    r = _run_doctor(
        project,
        env_extra={
            "HOME": str(home),
            "CLAUDE_HOME": str(home / ".claude"),
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "[OK] Python" in r.stdout
    assert "[OK] config present" in r.stdout
    assert "[OK] config parses" in r.stdout
    assert "[OK] PostToolUse hook wired" in r.stdout
    assert "[OK] evaluator agent" in r.stdout
    assert "[OK] skill bully present" in r.stdout


def test_doctor_missing_config_fails(tmp_path):
    # No .bully.yml in project
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    r = _run_doctor(
        project,
        env_extra={
            "HOME": str(home),
            "CLAUDE_HOME": str(home / ".claude"),
        },
    )
    assert r.returncode == 1
    assert "[FAIL] no .bully.yml" in r.stdout


def test_doctor_missing_hook_fails(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".bully.yml").write_text(
        "rules:\n"
        "  r1:\n"
        '    description: "d"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
    )
    home = tmp_path / "home"
    home.mkdir()
    r = _run_doctor(
        project,
        env_extra={
            "HOME": str(home),
            "CLAUDE_HOME": str(home / ".claude"),
        },
    )
    assert r.returncode == 1
    assert "[FAIL] no PostToolUse hook" in r.stdout


def test_doctor_missing_evaluator_agent_fails(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".bully.yml").write_text(
        "rules:\n"
        "  r1:\n"
        '    description: "d"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
    )
    # Provide the hook entry so we isolate the agent-missing case.
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"command": "hook.sh"}]}]}})
    )
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    r = _run_doctor(
        project,
        env_extra={
            "HOME": str(home),
            "CLAUDE_HOME": str(home / ".claude"),
        },
    )
    assert r.returncode == 1
    assert "[FAIL] evaluator agent missing" in r.stdout


def test_doctor_malformed_config_fails(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".bully.yml").write_text(
        "rules:\n"
        "\tbad-tabs:\n"  # tab indent -> parse error
        '    description: "x"\n'
        "    engine: script\n"
        '    scope: "*"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
    )
    home = tmp_path / "home"
    home.mkdir()
    r = _run_doctor(
        project,
        env_extra={
            "HOME": str(home),
            "CLAUDE_HOME": str(home / ".claude"),
        },
    )
    assert r.returncode == 1
    assert "[FAIL] config parse error" in r.stdout
