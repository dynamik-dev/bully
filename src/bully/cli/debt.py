"""`bully debt`: report `bully-disable-line` markers grouped by rule."""

from __future__ import annotations

import argparse
import fnmatch as _fnmatch
import re
import sys
from pathlib import Path

from bully.config.skip import effective_skip_patterns

# Distinct from the looser per-line `bully-disable:` directive. The debt
# command tracks an explicit, longer-form marker that requires a reason --
# `bully-disable-line <rule> reason: <text>` -- so authors can be held to
# a documentation bar without changing the looser real-time directive.
DEBT_DISABLE_RE = re.compile(
    r"bully-disable-line\s+(?P<rule>[a-zA-Z0-9_\-]+)\s*reason:\s*(?P<reason>.+?)\s*$"
)


def cmd_debt(config_path: str | None, strict: bool) -> int:
    """Walk the repo and report every `bully-disable-line` marker, grouped by rule."""
    path = config_path or ".bully.yml"
    cfg_abs = Path(path).resolve()
    if not cfg_abs.is_file():
        print(f"config not found: {path}", file=sys.stderr)
        return 1
    root = cfg_abs.parent
    skip_patterns = effective_skip_patterns(str(cfg_abs))

    findings: list[tuple[str, int, str, str]] = []  # (file, line, rule, reason)
    short_reasons: list[tuple[str, int, str, str]] = []

    for path_obj in root.rglob("*"):
        if not path_obj.is_file():
            continue
        rel = path_obj.relative_to(root).as_posix()
        if any(_fnmatch.fnmatchcase(rel, pat) for pat in skip_patterns):
            continue
        try:
            text = path_obj.read_text(errors="replace")
        except (OSError, PermissionError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            m = DEBT_DISABLE_RE.search(line)
            if not m:
                continue
            rule = m.group("rule")
            reason = m.group("reason").strip()
            findings.append((rel, i, rule, reason))
            if len(reason) < 12:
                short_reasons.append((rel, i, rule, reason))

    if not findings:
        print("No bully-disable-line markers found.")
        return 0

    by_rule: dict[str, list[tuple[str, int, str]]] = {}
    for f, ln, rule, reason in findings:
        by_rule.setdefault(rule, []).append((f, ln, reason))

    print(f"bully debt: {len(findings)} disable-line markers across {len(by_rule)} rules")
    for rule in sorted(by_rule):
        print(f"\n  {rule}: {len(by_rule[rule])} suppressions")
        for f, ln, reason in by_rule[rule]:
            print(f"    {f}:{ln}  reason: {reason}")

    if strict and short_reasons:
        print(
            f"\n{len(short_reasons)} markers have reasons shorter than 12 characters (strict mode):",
            file=sys.stderr,
        )
        for f, ln, rule, reason in short_reasons:
            print(f"  {f}:{ln}  [{rule}]  reason too short: {reason!r}", file=sys.stderr)
        return 2

    return 0


def cmd_debt_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bully debt")
    parser.add_argument("--config", default=".bully.yml")
    parser.add_argument("--strict", action="store_true", help="Fail if reasons are too short.")
    args = parser.parse_args(argv)
    return cmd_debt(args.config, args.strict)
