"""`bully stop` and `bully subagent-stop` subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bully.config.loader import parse_config
from bully.config.parser import ConfigError
from bully.config.scope import scope_glob_matches
from bully.state.telemetry import append_record, telemetry_path
from bully.state.trust import trust_status, untrusted_stderr


def cmd_stop(config_path: str | None) -> int:
    """Evaluate session-engine rules over the cumulative changed-set.

    Reads `.bully/session.jsonl` (append-only, one `{"file": ...}` per line,
    written by session-record on each edit). For each `engine: session` rule
    whose `when.changed_any` matched any file in the set, verify
    `require.changed_any` also matched at least one file. Otherwise the rule
    fires.

    Errors block (exit 2). On any non-blocking Stop (clean or warning-only)
    the session file is deleted so the next session starts fresh.
    """
    path = config_path or ".bully.yml"
    cfg_abs = Path(path).resolve()
    if not cfg_abs.is_file():
        return 0
    status, detail = trust_status(str(cfg_abs))
    if status != "trusted":
        sys.stderr.write(untrusted_stderr(str(cfg_abs), status, detail))
        return 0
    bully_dir = cfg_abs.parent / ".bully"
    session_file = bully_dir / "session.jsonl"
    if not session_file.exists():
        return 0
    seen: set[str] = set()
    changed: list[str] = []
    try:
        with open(session_file) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                fpath = rec.get("file") if isinstance(rec, dict) else None
                if isinstance(fpath, str) and fpath not in seen:
                    seen.add(fpath)
                    changed.append(fpath)
    except OSError:
        return 0
    if not changed:
        return 0

    try:
        rules = parse_config(str(cfg_abs))
    except ConfigError as e:
        sys.stderr.write(f"AGENTIC LINT -- config error: {e}\n")
        return 0
    session_rules = [r for r in rules if r.engine == "session"]

    def matches_any(globs: list[str]) -> bool:
        for c in changed:
            for pat in globs or []:
                if scope_glob_matches(pat, c):
                    return True
        return False

    violations: list[tuple[str, str, str]] = []
    for r in session_rules:
        when_globs = (r.when or {}).get("changed_any", []) or []
        if not isinstance(when_globs, list):
            when_globs = []
        if not matches_any(when_globs):
            continue
        require_globs = (r.require or {}).get("changed_any", []) or []
        if not isinstance(require_globs, list):
            require_globs = []
        if matches_any(require_globs):
            continue
        violations.append((r.id, r.severity, r.description))

    blocking = [v for v in violations if v[1] == "error"]
    if violations:
        sys.stderr.write("bully session check failed:\n")
        for rid, sev, desc in violations:
            sys.stderr.write(f"- [{sev}] {rid}: {desc}\n")
    if not blocking:
        # Reset session at any non-blocking Stop so the next session starts
        # fresh. Leaving session.jsonl in place on a warning-only stop would
        # re-fire the same warnings on every subsequent Stop.
        try:
            session_file.unlink()
        except FileNotFoundError:
            pass
        return 0
    return 2


def cmd_stop_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bully stop")
    parser.add_argument("--config", default=".bully.yml")
    args = parser.parse_args(argv)
    return cmd_stop(args.config)


def cmd_subagent_stop(config_path: str | None) -> int:
    """Append a subagent-completion telemetry record."""
    path = config_path or ".bully.yml"
    cfg_abs = Path(path).resolve()
    if not cfg_abs.is_file():
        return 0
    status, _ = trust_status(str(cfg_abs))
    if status != "trusted":
        return 0
    log_path = telemetry_path(path)
    if log_path is None:
        return 0
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "type": "subagent_stop",
    }
    append_record(log_path, record)
    return 0


def cmd_subagent_stop_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bully subagent-stop")
    parser.add_argument("--config", default=".bully.yml")
    args = parser.parse_args(argv)
    return cmd_subagent_stop(args.config)
