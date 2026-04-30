"""`bully session-start` and `bully session-record` subcommands."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from bully.config.loader import parse_config
from bully.config.parser import ConfigError
from bully.state.telemetry import append_record, telemetry_path
from bully.state.trust import trust_status


def cmd_session_start(config_path: str | None) -> int:
    """Tiny banner: 'bully active, N rules configured. Use `bully guide <file>`'.

    Also writes a `session_init` telemetry record stamping the producer
    version + schema version, so analyzer/forensics can attribute later
    records to a specific bully release.
    """
    from bully import BULLY_VERSION, TELEMETRY_SCHEMA_VERSION

    path = config_path or ".bully.yml"
    if not Path(path).is_file():
        return 0  # silent -- bully not configured here
    # Trust gate: refuse to parse rules, emit a banner, or stamp a
    # `session_init` telemetry record for an un-reviewed config. The hook
    # caller wraps this in a best-effort try/except, so a silent return 0
    # is the right shape.
    status, _ = trust_status(str(Path(path).resolve()))
    if status != "trusted":
        return 0
    try:
        rules = parse_config(path)
    except ConfigError:
        return 0  # silent on config error; the PostToolUse path will surface it
    if not rules:
        return 0
    print(
        f"bully active. {len(rules)} rules configured. "
        f"Run `bully guide <file>` to see rules that apply to a specific file."
    )
    log_path = telemetry_path(path)
    if log_path is not None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "type": "session_init",
            "bully_version": BULLY_VERSION,
            "schema_version": TELEMETRY_SCHEMA_VERSION,
        }
        try:
            append_record(log_path, record)
        except OSError:
            pass  # best-effort; telemetry writes never block session start
    return 0


def cmd_session_start_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bully session-start")
    parser.add_argument("--config", default=None, help="Path to .bully.yml")
    args = parser.parse_args(argv)
    return cmd_session_start(args.config)


def cmd_session_record(config_path: str | None, file_path: str) -> int:
    """Append `file_path` to the cumulative session changed-set."""
    path = config_path or ".bully.yml"
    cfg_abs = Path(path).resolve()
    if not cfg_abs.is_file():
        return 0
    status, _ = trust_status(str(cfg_abs))
    if status != "trusted":
        return 0
    bully_dir = cfg_abs.parent / ".bully"
    bully_dir.mkdir(exist_ok=True)
    session_file = bully_dir / "session.jsonl"
    line = json.dumps({"file": file_path}) + "\n"
    with open(session_file, "a") as f:
        f.write(line)
    return 0


def cmd_session_record_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bully session-record")
    parser.add_argument("--config", default=".bully.yml")
    parser.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    return cmd_session_record(args.config, args.file)
