"""Diff handling: context builders for hook payload, hunk analysis for can-match filters."""

from bully.diff.analysis import (
    can_match_diff,
    hunk_added_lines,
    hunk_removed_lines,
    rule_add_perspective,
)
from bully.diff.context import build_diff_context

__all__ = [
    "build_diff_context",
    "can_match_diff",
    "hunk_added_lines",
    "hunk_removed_lines",
    "rule_add_perspective",
]
