"""`bully doctor` subcommand: runtime + plugin diagnostic checks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from bully.config.loader import parse_config
from bully.config.parser import ConfigError, Rule
from bully.engines.ast_grep import AST_GREP_INSTALL_HINT, ast_grep_available
from bully.state.trust import trust_status

_MIN_PYTHON = (3, 10)


def check_python_version(version_info: tuple[int, int] = sys.version_info[:2]) -> tuple[bool, str]:
    """Return (ok, message) for the Python version check.

    Split out so tests can feed synthetic version tuples without spawning
    a different interpreter.
    """
    major, minor = version_info[:2]
    if (major, minor) >= _MIN_PYTHON:
        return True, f"[OK] Python {major}.{minor}"
    need = f"{_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}"
    return False, f"[FAIL] Python {major}.{minor} < {need} -- upgrade required"


def plugin_cache_candidates(resource_kind: str, name: str) -> list[Path]:
    """Return plausible `~/.claude/plugins/cache/*/bully/*/{skills,agents}/<name>/...` paths.

    resource_kind is "skills" or "agents". For skills, the file is `<name>/SKILL.md`;
    for agents, the file is `<name>.md` directly under `agents/`.
    """
    root = Path.home() / ".claude" / "plugins" / "cache"
    if not root.is_dir():
        return []
    pattern = f"*/bully/*/{resource_kind}/"
    out: list[Path] = []
    for base in root.glob(pattern):
        candidate = base / name / "SKILL.md" if resource_kind == "skills" else base / f"{name}.md"
        if candidate.is_file():
            out.append(candidate)
    return out


def cmd_doctor() -> int:
    ok = True

    py_ok, py_msg = check_python_version()
    print(py_msg)
    if not py_ok:
        ok = False

    cfg = Path.cwd() / ".bully.yml"
    if cfg.is_file():
        print(f"[OK] config present at {cfg}")
    else:
        print(f"[FAIL] no .bully.yml at {Path.cwd()}")
        ok = False

    parsed_rules: list[Rule] = []
    if cfg.is_file():
        try:
            parsed_rules = parse_config(str(cfg))
            print(f"[OK] config parses ({len(parsed_rules)} rules)")
        except ConfigError as e:
            print(f"[FAIL] config parse error: {e}")
            ok = False

    if cfg.is_file():
        status, detail = trust_status(str(cfg))
        if status == "trusted":
            print(f"[OK] config trusted on this machine ({detail})")
        elif status == "mismatch":
            print(
                f"[WARN] config trusted but checksum changed: {detail}. Run: bully trust --refresh"
            )
        else:
            print(
                f"[WARN] config not trusted on this machine ({detail}). "
                "Rules will not run until you run: bully trust"
            )

    ast_rule_count = sum(1 for r in parsed_rules if r.engine == "ast")
    if ast_rule_count > 0:
        if ast_grep_available():
            print(f"[OK] ast-grep on PATH ({ast_rule_count} engine:ast rule(s))")
        else:
            print(
                f"[FAIL] {ast_rule_count} engine:ast rule(s) need ast-grep. {AST_GREP_INSTALL_HINT}"
            )
            ok = False

    hook_wired = False
    for settings in (
        Path.cwd() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
    ):
        if not settings.is_file():
            continue
        try:
            data = json.loads(settings.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        hooks = data.get("hooks", {})
        entries = hooks.get("PostToolUse", [])
        if isinstance(entries, list):
            for entry in entries:
                for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                    if "hook.sh" in str(h.get("command", "")):
                        hook_wired = True
                        break
                if hook_wired:
                    break
        if hook_wired:
            print(f"[OK] PostToolUse hook wired in {settings}")
            break
    if not hook_wired:
        print("[FAIL] no PostToolUse hook invoking hook.sh found in .claude/settings.json")
        ok = False

    claude_home = Path(os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude")))
    agent_file = claude_home / "agents" / "bully-evaluator.md"
    plugin_agents = plugin_cache_candidates("agents", "bully-evaluator")
    if agent_file.is_file():
        print(f"[OK] evaluator agent at {agent_file}")
    elif plugin_agents:
        print(f"[OK] evaluator agent at {plugin_agents[0]} (plugin install)")
    else:
        print(
            f"[FAIL] evaluator agent missing -- expected at {agent_file} "
            f"or under ~/.claude/plugins/cache/*/bully/*/agents/bully-evaluator.md"
        )
        ok = False

    for suffix in (
        "bully",
        "bully-init",
        "bully-author",
        "bully-review",
    ):
        skill_md = Path.home() / ".claude" / "skills" / suffix / "SKILL.md"
        plugin_skill = plugin_cache_candidates("skills", suffix)
        if skill_md.is_file():
            print(f"[OK] skill {suffix} present")
        elif plugin_skill:
            print(f"[OK] skill {suffix} present at {plugin_skill[0]} (plugin install)")
        else:
            print(
                f"[FAIL] skill {suffix} missing -- expected at {skill_md} "
                f"or under ~/.claude/plugins/cache/*/bully/*/skills/{suffix}/SKILL.md"
            )
            ok = False

    return 0 if ok else 1
