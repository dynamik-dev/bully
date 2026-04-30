"""`bully explain <file>`: scope-only match analysis (distinct from --explain flag)."""

from __future__ import annotations

import argparse
import os
import sys

from bully.config.loader import parse_config
from bully.config.parser import ConfigError
from bully.config.scope import scope_glob_matches


def cmd_explain_subcommand(config_path: str | None, file_path: str) -> int:
    """Show every rule and whether/why it matches `file_path`.

    Distinct from the existing `--explain` flag (which prints per-rule
    pipeline verdicts after running the pipeline). This subcommand inspects
    only scope and prints which globs matched.
    """
    path = config_path or ".bully.yml"
    if not os.path.exists(path):
        print(f"No bully config found at {path}.", file=sys.stderr)
        return 1
    try:
        rules = parse_config(path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Match analysis for {file_path}:")
    for r in rules:
        scopes = list(r.scope) if r.scope else ["**"]
        matched_globs = [pat for pat in scopes if scope_glob_matches(pat, file_path)]
        if matched_globs:
            print(f"  MATCH  {r.id}  via {matched_globs}")
        else:
            print(f"  skip   {r.id}  scope={scopes}")
    return 0


def cmd_explain_subcommand_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="bully explain",
        description="Show why each rule matches or skips a file. "
        "Distinct from the `--explain` flag, which prints per-rule pipeline "
        "verdicts after running the pipeline.",
    )
    parser.add_argument("file", help="Path to a file (relative to cwd).")
    parser.add_argument("--config", default=None, help="Path to .bully.yml")
    args = parser.parse_args(argv)
    return cmd_explain_subcommand(args.config, args.file)
