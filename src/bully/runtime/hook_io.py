"""Hook-mode I/O: stdin payload reader, blocked-stderr formatter, prompt renderer."""

from __future__ import annotations

import json
import sys


def format_blocked_stderr(result: dict) -> str:
    """Render a blocked pipeline result as agent-readable text for stderr."""
    lines = ["AGENTIC LINT -- blocked. Fix these before proceeding:", ""]
    for v in result.get("violations", []):
        line = v.get("line")
        if line is None:
            header = f"- [{v['rule']}]: {v['description']}"
        else:
            header = f"- [{v['rule']}] line {line}: {v['description']}"
        lines.append(header)
        if v.get("suggestion"):
            lines.append(f"  suggestion: {v['suggestion']}")
    passed = result.get("passed", [])
    if passed:
        lines.append("")
        lines.append(f"Passed checks: {', '.join(passed)}")
    return "\n".join(lines) + "\n"


def read_stdin_payload() -> dict:
    """Read stdin; if JSON, return parsed dict, else wrap as raw diff."""
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return {"diff": raw}


def build_semantic_prompt(payload: dict) -> str:
    """Render the semantic evaluation payload as a human-readable prompt."""
    lines = [
        f"Evaluate this diff against the rules below. File: {payload.get('file', '?')}",
        "",
    ]
    passed = payload.get("passed_checks", [])
    if passed:
        lines.append(f"Already passed (do not re-evaluate): {', '.join(passed)}")
        lines.append("")
    lines.append("Rules to evaluate:")
    for r in payload.get("evaluate", []):
        lines.append(f"- [{r['id']}] ({r['severity']}): {r['description']}")
    lines.append("")
    lines.append("Diff:")
    lines.append(payload.get("diff", ""))
    lines.append("")
    lines.append(
        "For each violation: rule id, line number, description, fix suggestion. "
        "If no violations, say 'no violations' explicitly."
    )
    return "\n".join(lines)
