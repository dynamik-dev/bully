"""Tests for Write-mode large-file cap (plan 4.7)."""

from bully import (
    _cap_write_content,
    _was_write_truncated,
    _was_write_truncated_for_path,
    build_diff_context,
    run_pipeline,
)

RULE_YAML = (
    "rules:\n"
    "  semantic-rule:\n"
    '    description: "avoid whatever"\n'
    "    engine: semantic\n"
    '    scope: "*.py"\n'
    "    severity: warning\n"
)


def test_small_file_not_truncated():
    content = "\n".join(f"line-{i}" for i in range(1, 101))
    out = _cap_write_content(content)
    assert "truncated" not in out
    assert "line-1" in out
    assert "line-100" in out
    assert not _was_write_truncated(content)


def test_large_file_is_truncated():
    total = 500
    content = "\n".join(f"line-{i}" for i in range(1, total + 1))
    assert _was_write_truncated(content)
    out = _cap_write_content(content)
    assert "truncated" in out
    # First 100 lines kept
    assert "line-1" in out
    assert "line-100" in out
    # Last 50 lines kept
    assert "line-500" in out
    assert f"line-{total - 49}" in out
    # Middle lines dropped
    assert "line-250" not in out
    assert "line-300" not in out


def test_truncation_boundary_at_200_lines():
    # Exactly 200 lines -> not truncated (<=)
    content = "\n".join(f"line-{i}" for i in range(1, 201))
    assert not _was_write_truncated(content)
    # 201 lines -> truncated
    content_big = "\n".join(f"line-{i}" for i in range(1, 202))
    assert _was_write_truncated(content_big)


def test_write_mode_build_diff_context_caps(tmp_path):
    target = tmp_path / "big.py"
    total = 400
    target.write_text("\n".join(f"x{i}" for i in range(1, total + 1)))

    out = build_diff_context(
        tool_name="Write",
        file_path=str(target),
        old_string="",
        new_string="",
    )
    assert "truncated" in out
    assert "x1" in out
    assert f"x{total}" in out


def test_was_write_truncated_for_path(tmp_path):
    p = tmp_path / "big.py"
    p.write_text("\n".join(f"L{i}" for i in range(1, 300)))
    assert _was_write_truncated_for_path(str(p))

    small = tmp_path / "small.py"
    small.write_text("L1\nL2\n")
    assert not _was_write_truncated_for_path(str(small))


def test_pipeline_marks_write_content_truncated(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "big.py"
    total = 300
    target.write_text("\n".join(f"x = {i}" for i in range(1, total + 1)))

    # Provide a multi-line diff so the semantic rule dispatches (not filtered).
    diff = "@@ -1,2 +1,4 @@\n+added one\n+added two\n"
    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        diff,
    )
    assert result["status"] == "evaluate"
    assert result.get("write_content") == "truncated"
