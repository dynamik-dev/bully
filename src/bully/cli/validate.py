"""`bully validate` and `bully show-resolved-config` subcommands."""

from __future__ import annotations

import os
import subprocess
import sys

from bully.config.loader import parse_config
from bully.config.parser import ConfigError, Rule
from bully.engines.ast_grep import AST_GREP_INSTALL_HINT, ast_grep_available


def cmd_validate(config_path: str | None, *, execute_dry_run: bool = False) -> int:
    path = config_path or ".bully.yml"
    if not os.path.exists(path):
        print(f"[FAIL] config not found: {path}", file=sys.stderr)
        return 1
    try:
        rules = parse_config(path)
    except ConfigError as e:
        print(f"[FAIL] {path}: {e}", file=sys.stderr)
        return 1
    print(f"[OK] parsed {len(rules)} rule(s) from {path}")
    for r in rules:
        print(f"  - {r.id}  engine={r.engine}  severity={r.severity}  scope={list(r.scope)}")
    ast_rule_ids = [r.id for r in rules if r.engine == "ast"]
    if ast_rule_ids and not ast_grep_available():
        print(
            f"[WARN] {len(ast_rule_ids)} engine:ast rule(s) will be skipped at runtime: "
            f"ast-grep not on PATH. {AST_GREP_INSTALL_HINT}",
            file=sys.stderr,
        )
    if execute_dry_run:
        return run_execute_dry_run(rules)
    return 0


def run_execute_dry_run(rules: list[Rule]) -> int:
    """Execute every script rule against `/dev/null`, report broken scripts.

    Catches shell/regex-level errors at config time: unbalanced parens in a
    `grep -E` pattern, typos in command names, non-executable scripts, etc.
    A rule is flagged as broken when either:

    - The exit code is not in {0, 1} (2 = grep syntax error, 126 = not
      executable, 127 = command-not-found, etc.), OR
    - stderr carries a known tool-error signature even when exit is 0/1.
      This matters because shells often mask inner errors: `grep ... &&
      exit 1 || exit 0` swallows grep's exit-2 and reports 0, leaving the
      regex diagnostic only in stderr.

    Returns 0 if all script rules are healthy, 1 if any were flagged.
    """
    error_signatures = (
        "grep:",
        "sed:",
        "awk:",
        "bash:",
        "sh:",
        "command not found",
        "syntax error",
        "not recognized as an internal",
    )

    script_rules = [r for r in rules if r.engine == "script" and r.script]
    if not script_rules:
        print("[OK] no script rules to dry-run")
        return 0

    failures = 0
    for rule in script_rules:
        cmd = rule.script.replace("{file}", "/dev/null")
        try:
            result = subprocess.run(  # bully-disable: no-shell-true-subprocess dry-run probe of user-configured script; mirrors real execute_script_rule path
                cmd,
                shell=True,
                timeout=5,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            print(f"[WARN] {rule.id}: dry-run exit=timeout stderr: script timed out")
            failures += 1
            continue

        rc = result.returncode
        stderr = result.stderr.strip()
        stderr_first = stderr.splitlines()[0] if stderr else ""
        stderr_looks_broken = any(sig in stderr.lower() for sig in error_signatures)

        if rc in (0, 1) and not stderr_looks_broken:
            print(f"[OK] {rule.id}: dry-run clean (exit {rc})")
            continue
        failures += 1
        print(f"[WARN] {rule.id}: dry-run exit={rc} stderr: {stderr_first}")

    return 0 if failures == 0 else 1


def cmd_show_resolved(config_path: str | None) -> int:
    path = config_path or ".bully.yml"
    try:
        rules = parse_config(path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for r in rules:
        print(
            f"{r.id}\tengine={r.engine}\tseverity={r.severity}\t"
            f"scope={','.join(r.scope)}\tfix_hint={r.fix_hint or ''}"
        )
    return 0
