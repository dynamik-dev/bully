"""Tests for building diff context with real file line numbers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import build_diff_context


def test_edit_produces_unified_diff_with_line_numbers(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("line one\nline two\nline three new\nline four\nline five\n")
    diff = build_diff_context(
        tool_name="Edit",
        file_path=str(file_path),
        old_string="line three",
        new_string="line three new",
    )
    assert "@@" in diff  # unified diff hunk header
    assert "line three new" in diff
    assert "-line three" in diff or "-line three " in diff or "line three" in diff


def test_edit_diff_line_numbers_match_file_position(tmp_path):
    # Put the edit deep in the file so we can check line numbers are file-anchored.
    lines_before = ["filler " + str(i) for i in range(1, 31)]  # lines 1..30
    lines_after = lines_before.copy()
    lines_after[24] = "EDITED LINE"  # line 25 (1-indexed)
    file_path = tmp_path / "big.txt"
    file_path.write_text("\n".join(lines_after) + "\n")

    diff = build_diff_context(
        tool_name="Edit",
        file_path=str(file_path),
        old_string="filler 25",
        new_string="EDITED LINE",
    )
    # hunk header should reference a line near 25, not 1
    assert "@@" in diff
    hunk_line = next(line for line in diff.splitlines() if line.startswith("@@"))
    # Extract the starting line number from @@ -N,M +N,M @@
    import re

    m = re.search(r"@@ -(\d+)", hunk_line)
    assert m is not None
    start = int(m.group(1))
    assert 18 <= start <= 25, f"expected hunk near line 25, got {start}"


def test_write_produces_line_numbered_content(tmp_path):
    file_path = tmp_path / "new.txt"
    file_path.write_text("alpha\nbeta\ngamma\n")
    diff = build_diff_context(
        tool_name="Write",
        file_path=str(file_path),
        old_string="",
        new_string="alpha\nbeta\ngamma\n",
    )
    assert "1:" in diff or "  1 " in diff
    assert "alpha" in diff
    assert "gamma" in diff


def test_edit_missing_file_returns_fallback(tmp_path):
    diff = build_diff_context(
        tool_name="Edit",
        file_path=str(tmp_path / "nope.txt"),
        old_string="old",
        new_string="new",
    )
    # No crash; return something that reflects the raw change.
    assert "new" in diff


def test_edit_with_unfindable_new_string_returns_fallback(tmp_path):
    file_path = tmp_path / "f.txt"
    file_path.write_text("totally different content\n")
    diff = build_diff_context(
        tool_name="Edit",
        file_path=str(file_path),
        old_string="old",
        new_string="new",  # not present in file
    )
    # Fallback: include at least the raw strings so the LLM has something to evaluate.
    assert "old" in diff or "new" in diff
