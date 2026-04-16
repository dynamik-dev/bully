"""Tests for parse_script_output adapters across common tool formats."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import parse_script_output


def test_grep_n_line_content_format():
    out = "42:    return compact('result');\n"
    vs = parse_script_output("r", "error", out)
    assert len(vs) == 1
    assert vs[0].line == 42
    assert "compact" in vs[0].description


def test_file_line_col_message_format_eslint_style():
    out = "src/foo.ts:12:5: error: Unexpected any.\n"
    vs = parse_script_output("no-any", "error", out)
    assert len(vs) == 1
    assert vs[0].line == 12
    assert "any" in vs[0].description.lower()


def test_file_line_message_format_compiler_style():
    out = "src/bar.py:87: error: Missing type annotation\n"
    vs = parse_script_output("mypy", "error", out)
    assert len(vs) == 1
    assert vs[0].line == 87
    assert "annotation" in vs[0].description.lower()


def test_phpstan_style_file_line_message():
    out = "src/Foo.php:45: Method returns mixed, should return int.\n"
    vs = parse_script_output("phpstan", "error", out)
    assert len(vs) == 1
    assert vs[0].line == 45


def test_multiple_mixed_lines():
    out = "src/a.ts:10:3: Unused variable x\nsrc/a.ts:22:7: Missing return type\n"
    vs = parse_script_output("eslint", "error", out)
    assert len(vs) == 2
    assert vs[0].line == 10
    assert vs[1].line == 22


def test_json_violations_single_object():
    out = '{"line": 7, "message": "forbidden pattern"}\n'
    vs = parse_script_output("r", "error", out)
    assert len(vs) == 1
    assert vs[0].line == 7
    assert "forbidden" in vs[0].description


def test_json_violations_array():
    out = '[{"line": 3, "message": "a"}, {"line": 9, "message": "b"}]\n'
    vs = parse_script_output("r", "error", out)
    assert len(vs) == 2
    assert vs[0].line == 3
    assert vs[1].line == 9


def test_unrecognized_output_still_produces_violation():
    out = "something completely unexpected\n"
    vs = parse_script_output("r", "error", out)
    # Don't drop information silently -- return at least one violation.
    assert len(vs) >= 1
    assert "unexpected" in vs[0].description.lower() or vs[0].description


def test_empty_output_returns_empty():
    assert parse_script_output("r", "error", "") == []
    assert parse_script_output("r", "error", "   \n\n") == []
