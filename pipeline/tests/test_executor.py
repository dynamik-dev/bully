"""Tests for script rule execution and output parsing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import Rule, execute_script_rule, parse_script_output

FIXTURES = Path(__file__).parent / "fixtures"


def test_passing_rule_returns_no_violations():
    rule = Rule(
        id="no-compact",
        description="No compact()",
        engine="script",
        scope="*.php",
        severity="error",
        script="grep -n 'compact(' {file} && exit 1 || exit 0",
    )
    violations = execute_script_rule(rule, str(FIXTURES / "clean.php"), "")
    assert violations == []


def test_failing_rule_returns_violations():
    rule = Rule(
        id="no-compact",
        description="No compact()",
        engine="script",
        scope="*.php",
        severity="error",
        script="grep -n 'compact(' {file} && exit 1 || exit 0",
    )
    violations = execute_script_rule(rule, str(FIXTURES / "violation.php"), "")
    assert len(violations) >= 1
    assert violations[0].rule == "no-compact"
    assert violations[0].engine == "script"
    assert violations[0].severity == "error"
    assert violations[0].line is not None


def test_failing_rule_captures_line_number():
    rule = Rule(
        id="no-db-facade",
        description="No DB::",
        engine="script",
        scope="*.php",
        severity="error",
        script="grep -n 'DB::' {file} && exit 1 || exit 0",
    )
    violations = execute_script_rule(rule, str(FIXTURES / "violation.php"), "")
    assert len(violations) >= 1
    assert isinstance(violations[0].line, int)
    assert violations[0].line > 0


def test_parse_grep_output():
    output = "42:    return compact('name', 'email');\n"
    violations = parse_script_output("test-rule", "error", output)
    assert len(violations) == 1
    assert violations[0].line == 42
    assert "compact" in violations[0].description


def test_parse_multiline_grep_output():
    output = "10:    compact('a');\n25:    compact('b');\n"
    violations = parse_script_output("test-rule", "error", output)
    assert len(violations) == 2
    assert violations[0].line == 10
    assert violations[1].line == 25


def test_parse_empty_output_returns_empty():
    violations = parse_script_output("test-rule", "error", "")
    assert violations == []


def test_warning_severity_preserved():
    rule = Rule(
        id="style-check",
        description="Style warning",
        engine="script",
        scope="*.php",
        severity="warning",
        script="grep -n 'compact(' {file} && exit 1 || exit 0",
    )
    violations = execute_script_rule(rule, str(FIXTURES / "violation.php"), "")
    assert len(violations) >= 1
    assert violations[0].severity == "warning"
