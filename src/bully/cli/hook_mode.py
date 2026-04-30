"""PostToolUse hook driver: parse stdin payload, run pipeline, render hook output."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bully.cli.session import cmd_session_record
from bully.config.parser import ConfigError
from bully.diff.context import build_diff_context
from bully.runtime.hook_io import format_blocked_stderr, read_stdin_payload
from bully.runtime.runner import run_pipeline
from bully.state.trust import untrusted_stderr


def find_config_upward(start: Path) -> Path | None:
    cur = start.resolve()
    if cur.is_file():
        cur = cur.parent
    for p in (cur, *cur.parents):
        candidate = p / ".bully.yml"
        if candidate.is_file():
            return candidate
    return None


def run_hook_mode() -> int:
    """Read stdin JSON from Claude Code, run the pipeline, emit hook output."""
    payload = read_stdin_payload()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    file_path = tool_input.get("file_path") or payload.get("file_path") or ""
    old_string = tool_input.get("old_string", "") or payload.get("old_string", "") or ""
    if tool_name == "Write":
        new_string = (
            tool_input.get("content")
            or tool_input.get("new_string")
            or payload.get("content")
            or payload.get("new_string")
            or ""
        )
    else:
        new_string = tool_input.get("new_string", "") or payload.get("new_string", "") or ""

    if not file_path or not Path(file_path).is_file():
        return 0

    config = find_config_upward(Path(file_path))
    if config is None:
        return 0

    # Append the touched file to the cumulative session changed-set so engine:
    # session rules can see it at Stop time. Record the path relative to the
    # config root when possible so user-visible globs match. Best-effort:
    # never let session-record block the post-tool flow.
    try:
        try:
            rel = str(Path(file_path).resolve().relative_to(Path(config).resolve().parent))
        except ValueError:
            rel = file_path
        cmd_session_record(str(config), rel)
    except Exception:
        pass

    diff = build_diff_context(
        tool_name=tool_name,
        file_path=file_path,
        old_string=old_string,
        new_string=new_string,
    )

    try:
        result = run_pipeline(str(config), file_path, diff)
    except ConfigError as e:
        sys.stderr.write(f"AGENTIC LINT -- config error: {e}\n")
        return 0

    status = result.get("status", "pass")
    if status == "untrusted":
        sys.stderr.write(
            untrusted_stderr(
                result.get("config", str(config)),
                result.get("trust_status", "untrusted"),
                result.get("trust_detail", ""),
            )
        )
        return 0
    if status == "blocked":
        sys.stderr.write(format_blocked_stderr(result))
        return 2
    if status == "evaluate":
        # Forward the dict run_pipeline already produced. `_evaluator_input`
        # was built from the unstripped rules (with `_excerpt`) inside
        # `build_semantic_payload_dict`, so it carries `<EXCERPT_FOR_RULE>`
        # blocks. Re-rendering here from the outer `evaluate` array would
        # drop them.
        out_payload = {
            "file": result.get("file", file_path),
            "diff": result.get("diff", diff),
            "passed_checks": result.get("passed_checks", []),
            "evaluate": result.get("evaluate", []),
            "_evaluator_input": result.get("_evaluator_input", ""),
        }
        if "line_anchors" in result:
            out_payload["line_anchors"] = result["line_anchors"]
        ctx = "AGENTIC LINT SEMANTIC EVALUATION REQUIRED:\n\n" + json.dumps(out_payload, indent=2)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": ctx,
                    }
                }
            )
        )
    return 0
