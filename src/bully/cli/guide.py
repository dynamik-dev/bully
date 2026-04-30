"""`bully guide <file>`: list rules whose scope matches a path, with descriptions."""

from __future__ import annotations

import argparse
import os
import sys

from bully.config.loader import parse_config
from bully.config.parser import ConfigError
from bully.config.scope import filter_rules


def cmd_guide(config_path: str | None, file_path: str) -> int:
    """List rules whose scope matches `file_path`, with descriptions."""
    path = config_path or ".bully.yml"
    if not os.path.exists(path):
        print(f"No bully config found at {path}.", file=sys.stderr)
        return 1
    try:
        rules = parse_config(path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    matched = filter_rules(rules, file_path)
    if not matched:
        print(f"No bully rules apply to {file_path}.")
        return 0
    print(f"Rules in scope for {file_path} ({len(matched)}):")
    for r in matched:
        print(f"\n  [{r.severity}] {r.id} ({r.engine})")
        for line in r.description.splitlines():
            print(f"      {line}")
    return 0


def cmd_guide_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="bully guide",
        description="Show rules in scope for a file.",
    )
    parser.add_argument("file", help="Path to a file (relative to cwd).")
    parser.add_argument("--config", default=None, help="Path to .bully.yml")
    args = parser.parse_args(argv)
    return cmd_guide(args.config, args.file)
