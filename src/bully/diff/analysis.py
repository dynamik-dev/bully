"""Diff hunk analysis: can-match filters and excerpt builders for semantic payloads."""

from __future__ import annotations

import re
from pathlib import Path

from bully.config.parser import Rule

_COMMENT_LINE_RE = re.compile(r"^\s*(?://|#|--)|^\s*/\*|^\s*\*/|^\s*\*\s")

# Word-boundary matcher for "avoid X being added" rule descriptions. Trigger
# words ("avoid", "no", "ban", "don't"/"dont", "forbid") match only as whole
# tokens. The earlier substring-based matcher false-flagged "banner" via
# "ban", "avoidance" via "avoid", and "no-op" via "no-"; word boundaries fix
# the first two, and the negative lookahead on "no" rejects hyphenated
# compounds like "no-op" so they aren't read as imperative "no X" rules.
_ADD_PERSPECTIVE_RE = re.compile(
    r"\b(?:avoid|ban|forbid|don'?t)\b|\bno\b(?!-)",
    re.IGNORECASE,
)


def hunk_added_lines(diff: str) -> list[str]:
    """Return lines added in the diff (lines starting with `+` but not `+++`)."""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            out.append(line[1:])
    return out


def hunk_removed_lines(diff: str) -> list[str]:
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("---"):
            continue
        if line.startswith("-"):
            out.append(line[1:])
    return out


def _all_whitespace(lines: list[str]) -> bool:
    return all(not line.strip() for line in lines)


def _all_comment(lines: list[str]) -> bool:
    if not lines:
        return False
    return all(_COMMENT_LINE_RE.match(line) or not line.strip() for line in lines)


def rule_add_perspective(description: str) -> bool:
    """True if `description` reads like an "avoid X being added" rule.

    Trigger words ("avoid", "no", "ban", "don't"/"dont", "forbid") must
    match as whole tokens via word boundaries. Used by `can_match_diff`
    to skip pure-deletion diffs against rules that only fire when something
    new is introduced.
    """
    return _ADD_PERSPECTIVE_RE.search(description) is not None


def can_match_diff(rule: Rule, diff: str) -> tuple[bool, str]:
    """Return (should_evaluate, skip_reason_if_not).

    Cheap pre-dispatch gate that drops diffs that *can't* match a semantic
    rule -- empty diffs, whitespace-only adds, comment-only adds (for rules
    not about comments), and pure deletions for "avoid X" rules.
    """
    if not diff.strip():
        return False, "empty-diff"

    added = hunk_added_lines(diff)
    removed = hunk_removed_lines(diff)

    if added and _all_whitespace(added):
        return False, "whitespace-only-additions"

    if added and _all_comment(added) and "comment" not in rule.description.lower():
        return False, "comment-only-additions"

    if not added and removed and rule_add_perspective(rule.description):
        return False, "pure-deletion-add-perspective-rule"

    return True, ""


def build_excerpt(file_path: str, diff: str, lines: int) -> str | None:
    """Return a bounded excerpt of `file_path` around the diff hunks.

    Reads `lines` rows above and below each hunk on disk, capped to file
    bounds. Multiple hunks are merged when their windows overlap. Returns
    None if the file cannot be read or the diff has no parseable hunks.
    """
    if lines <= 0:
        return None
    try:
        text = Path(file_path).read_text(errors="replace").splitlines()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return None

    hunk_starts: list[int] = []
    for line in diff.splitlines():
        if line.startswith("@@"):
            try:
                plus = line.split("+", 1)[1]
                start = int(plus.split(",", 1)[0].split(" ", 1)[0])
                hunk_starts.append(start)
            except (IndexError, ValueError):
                continue
    if not hunk_starts:
        return None

    spans: list[tuple[int, int]] = []
    for start in hunk_starts:
        lo = max(1, start - lines)
        hi = min(len(text), start + lines)
        if hi >= lo:
            spans.append((lo, hi))

    if not spans:
        return None

    spans.sort()
    merged: list[tuple[int, int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))

    out: list[str] = []
    for lo, hi in merged:
        out.append(f"--- excerpt {file_path}:{lo}-{hi} ---")
        for i in range(lo, hi + 1):
            out.append(f"{i:6d}  {text[i - 1]}")
    return "\n".join(out)
