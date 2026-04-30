"""Argument parser + verb-shorthand normalization for the bully CLI."""

from __future__ import annotations

import argparse

# Subcommand verbs accepted as the first argv element. Each maps to either a
# flag or a small argv rewrite. Keeps the legacy `--validate`/`--doctor` flags
# and the legacy positional `<config> <file>` form (used by hook.sh) working.
_SUBCOMMAND_FLAGS = {
    "validate": "--validate",
    "doctor": "--doctor",
    "show-resolved-config": "--show-resolved-config",
    "baseline-init": "--baseline-init",
    "trust": "--trust",
}


def normalize_argv(argv: list[str]) -> list[str]:
    """Translate `bully <verb> ...` shorthand into the underlying flag form.

    - `validate` / `doctor` / `show-resolved-config` / `baseline-init` / `trust`
      become their `--verb` flag equivalents.
    - `lint <path>` becomes `--file <path>` (the rest of argv is preserved).
    - Anything else passes through unchanged so legacy positional and flag
      invocations keep working.
    """
    if not argv:
        return argv
    head = argv[0]
    if head in _SUBCOMMAND_FLAGS:
        return [_SUBCOMMAND_FLAGS[head], *argv[1:]]
    if head == "lint":
        rest = argv[1:]
        if rest and not rest[0].startswith("-"):
            return ["--file", rest[0], *rest[1:]]
        return rest
    return argv


def parse_args(argv: list[str]) -> argparse.Namespace:
    argv = normalize_argv(argv)
    parser = argparse.ArgumentParser(
        prog="bully",
        description="Agentic Lint pipeline. Runs script and semantic rules for a file.",
    )
    parser.add_argument("positional", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("--config", help="Path to .bully.yml")
    parser.add_argument("--file", dest="file_path", help="Target file to evaluate")
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        help="Evaluate only this rule id. Repeatable.",
    )
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="Print the LLM prompt text for the semantic payload instead of JSON.",
    )
    parser.add_argument("--diff", help="Inline diff string (bypasses stdin).")
    parser.add_argument(
        "--hook-mode",
        action="store_true",
        help="Read tool-hook JSON on stdin and emit Claude Code hook output.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the config file: parse, check enums, exit nonzero on error.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run diagnostic checks and exit.",
    )
    parser.add_argument(
        "--show-resolved-config",
        action="store_true",
        help="Print merged rules (after resolving extends) as compact text.",
    )
    parser.add_argument(
        "--baseline-init",
        action="store_true",
        help="Run the pipeline over a glob and write current violations to baseline.json.",
    )
    parser.add_argument(
        "--glob",
        default=None,
        help="Glob pattern for --baseline-init (relative to config dir).",
    )
    parser.add_argument(
        "--log-verdict",
        action="store_true",
        help="Append a semantic_verdict telemetry record.",
    )
    parser.add_argument("--verdict", choices=("pass", "violation"), default=None)
    parser.add_argument(
        "--trust",
        action="store_true",
        help="Allow the given --config to execute rules on this machine. "
        "Records a SHA256 checksum; edits to the config re-require --trust.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="With --trust: re-approve a changed config. Without --trust: no-op.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print per-rule verdict (fire/pass/skipped <reason>/dispatched) for "
        "every rule in scope, instead of the JSON pipeline result.",
    )
    parser.add_argument(
        "--execute-dry-run",
        dest="execute_dry_run",
        action="store_true",
        help="With --validate: run each script rule against empty input to catch "
        "shell/regex-level errors (unbalanced parens, missing commands) at config time.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="For CI-style callers. Exit non-zero on any non-'pass' status "
        "(untrusted, blocked, config error). Default is advisory: untrusted "
        "exits 0 so the PostToolUse hook never blocks edits on infra issues.",
    )
    args = parser.parse_args(argv)
    # Back-compat: accept positional args (used by hook)
    if args.positional and not args.config:
        args.config = args.positional[0]
    if len(args.positional) >= 2 and not args.file_path:
        args.file_path = args.positional[1]
    return args
