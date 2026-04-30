"""Diff context builder for hook payloads.

Anchors Edit/Write tool calls to the file on disk, producing unified diffs with
real line numbers when possible and falling back to synthetic diffs (with a
warning marker) when anchoring fails.
"""

from __future__ import annotations

import difflib

# Write-mode content cap markers.
_WRITE_HEAD_LINES = 100
_WRITE_TAIL_LINES = 50
_WRITE_MAX_LINES = 200

# Synthetic-line warning marker.
SYNTHETIC_MARKER = "# WARNING: synthetic line numbers -- could not anchor diff to file on disk"


def build_diff_context(
    tool_name: str,
    file_path: str,
    old_string: str,
    new_string: str,
    context_lines: int = 5,
) -> str:
    """Produce a diff with real file line numbers for the semantic payload.

    Falls back to a synthetic diff (with a warning marker) when anchoring fails.
    For Write mode, caps very large files to head+tail slices.
    """
    try:
        with open(file_path) as f:
            current = f.read()
    except OSError:
        if tool_name == "Write":
            return cap_write_content(new_string)
        return (
            f"{SYNTHETIC_MARKER}\n"
            f"--- {file_path} (file not readable)\n+++ edit\n-{old_string}\n+{new_string}\n"
        )

    if tool_name == "Write":
        return cap_write_content(current)

    if new_string and new_string in current:
        before = current.replace(new_string, old_string, 1)
    elif old_string and old_string in current:
        before = current
        current = current.replace(old_string, new_string, 1)
    else:
        before_lines = (old_string or "").splitlines(keepends=True) or ["\n"]
        after_lines = (new_string or "").splitlines(keepends=True) or ["\n"]
        synth = "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"{file_path}.before",
                tofile=f"{file_path}.after",
                n=context_lines,
            )
        )
        return SYNTHETIC_MARKER + "\n" + synth

    before_lines = before.splitlines(keepends=True)
    after_lines = current.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{file_path}.before",
            tofile=f"{file_path}.after",
            n=context_lines,
        )
    )


def cap_write_content(content: str) -> str:
    """Return line-numbered content; if too long, slice head + tail with a marker."""
    lines = content.splitlines()
    total = len(lines)
    if total <= _WRITE_MAX_LINES:
        return _line_number(content)

    width = max(3, len(str(total)))
    head = lines[:_WRITE_HEAD_LINES]
    tail = lines[total - _WRITE_TAIL_LINES :]
    out: list[str] = []
    for i, line in enumerate(head, start=1):
        out.append(f"{i:>{width}}: {line}")
    truncated = total - _WRITE_HEAD_LINES - _WRITE_TAIL_LINES
    out.append(f"... {truncated} lines truncated ...")
    tail_start = total - _WRITE_TAIL_LINES + 1
    for i, line in enumerate(tail, start=tail_start):
        out.append(f"{i:>{width}}: {line}")
    return "\n".join(out)


def was_write_truncated(content: str) -> bool:
    return len(content.splitlines()) > _WRITE_MAX_LINES


def was_write_truncated_for_path(file_path: str) -> bool:
    """Cheap stat-only check that doesn't re-read huge files into memory unnecessarily."""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            count = sum(1 for _ in f)
        return count > _WRITE_MAX_LINES
    except OSError:
        return False


def _line_number(content: str) -> str:
    """Prefix each line with `NNNN:` for line-anchored evaluation."""
    lines = content.splitlines()
    width = max(3, len(str(len(lines))))
    return "\n".join(f"{i:>{width}}: {line}" for i, line in enumerate(lines, start=1))
