"""bully CLI: dispatcher for subcommands and the default flag-driven flow.

`main()` is the entry point for `python -m bully`, the installed `bully`
console script, and `hooks/hook.sh`.
"""

from __future__ import annotations

import json
import os
import sys

from bully.cli.args import parse_args
from bully.cli.baseline import cmd_baseline_init
from bully.cli.coverage import cmd_coverage_main
from bully.cli.debt import cmd_debt_main
from bully.cli.doctor import cmd_doctor
from bully.cli.explain import cmd_explain_subcommand_main
from bully.cli.guide import cmd_guide_main
from bully.cli.hook_mode import run_hook_mode
from bully.cli.log_verdict import cmd_log_verdict
from bully.cli.session import cmd_session_record_main, cmd_session_start_main
from bully.cli.stop import cmd_stop_main, cmd_subagent_stop_main
from bully.cli.validate import cmd_show_resolved, cmd_validate
from bully.config.parser import ConfigError
from bully.diff.context import build_diff_context
from bully.runtime.hook_io import build_semantic_prompt, format_blocked_stderr, read_stdin_payload
from bully.runtime.runner import print_explain, run_pipeline
from bully.state.trust import cmd_trust, untrusted_stderr


def main() -> None:
    # Subcommand short-circuits (positional dispatch). These bypass the main
    # parser, which uses a flat flag model, so positional commands don't get
    # rejected and the `bully explain <file>` subcommand doesn't collide
    # with the `--explain` flag.
    if len(sys.argv) >= 2 and sys.argv[1] == "bench":
        from bully.bench import main as bench_main  # noqa: PLC0415

        sys.exit(bench_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "guide":
        sys.exit(cmd_guide_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "explain":
        sys.exit(cmd_explain_subcommand_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "session-start":
        sys.exit(cmd_session_start_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "stop":
        sys.exit(cmd_stop_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "subagent-stop":
        sys.exit(cmd_subagent_stop_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "session-record":
        sys.exit(cmd_session_record_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "coverage":
        sys.exit(cmd_coverage_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "debt":
        sys.exit(cmd_debt_main(sys.argv[2:]))

    args = parse_args(sys.argv[1:])

    if args.trust:
        sys.exit(cmd_trust(args.config, refresh=args.refresh))
    if args.validate:
        sys.exit(cmd_validate(args.config, execute_dry_run=args.execute_dry_run))
    if args.doctor:
        sys.exit(cmd_doctor())
    if args.show_resolved_config:
        sys.exit(cmd_show_resolved(args.config))
    if args.baseline_init:
        sys.exit(cmd_baseline_init(args.config, args.glob))
    if args.log_verdict:
        if not args.rule or not args.verdict:
            print(
                "usage: --log-verdict --rule RULE_ID --verdict pass|violation [--file PATH]",
                file=sys.stderr,
            )
            sys.exit(1)
        rule_id = args.rule[0] if args.rule else ""
        sys.exit(cmd_log_verdict(args.config, rule_id, args.verdict, args.file_path))
    if args.hook_mode:
        sys.exit(run_hook_mode())

    # Default config to ./.bully.yml when a target file is given but no
    # config is specified -- lets `bully lint src/foo.py` work standalone.
    if args.file_path and not args.config and os.path.exists(".bully.yml"):
        args.config = ".bully.yml"

    if not args.config or not args.file_path:
        print(
            json.dumps({"error": "Usage: bully lint <file> [--config <path>]"}),
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = args.config
    file_path = args.file_path

    if not os.path.exists(config_path):
        print(json.dumps({"status": "pass", "file": file_path, "reason": "no config found"}))
        sys.exit(0)

    if args.diff is not None:
        diff = args.diff
    else:
        payload = read_stdin_payload()
        if "diff" in payload:
            diff = payload["diff"]
        elif "tool_name" in payload:
            tool_input = (
                payload.get("tool_input", {}) if isinstance(payload.get("tool_input"), dict) else {}
            )
            diff = build_diff_context(
                tool_name=payload.get("tool_name", ""),
                file_path=tool_input.get("file_path") or payload.get("file_path", file_path),
                old_string=tool_input.get("old_string") or payload.get("old_string", ""),
                new_string=(
                    tool_input.get("content")
                    or tool_input.get("new_string")
                    or payload.get("new_string", "")
                ),
            )
        else:
            diff = ""

    try:
        result = run_pipeline(
            config_path,
            file_path,
            diff,
            rule_filter=set(args.rule) if args.rule else None,
            include_skipped=args.explain,
        )
    except ConfigError as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        sys.exit(1)

    if args.explain:
        print_explain(result, file_path)
        return

    if args.print_prompt:
        if result.get("status") == "evaluate":
            print(build_semantic_prompt(result))
        else:
            print(
                json.dumps(
                    {
                        "note": "No semantic evaluation to print (status is not 'evaluate').",
                        "result": result,
                    },
                    indent=2,
                )
            )
        return

    print(json.dumps(result, indent=2))

    if result.get("status") == "untrusted":
        sys.stderr.write(
            untrusted_stderr(
                result.get("config", config_path),
                result.get("trust_status", "untrusted"),
                result.get("trust_detail", ""),
            )
        )
        sys.exit(3 if args.strict else 0)
    if result.get("status") == "blocked":
        sys.stderr.write(format_blocked_stderr(result))
        sys.exit(2)
    if args.strict and result.get("status") not in (None, "pass", "evaluate"):
        sys.exit(3)


__all__ = ["main"]
