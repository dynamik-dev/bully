"""Tests for parse_script_output's stateful continuation-joining.

Field report from a live user session (see CHANGELOG v0.6.1): phpstan's
indented columnar output and pest's wrapped failures produced a single
"line ?" violation whose description was 500 chars of separator noise,
truncated mid-word. These tests pin the new behavior:

- indented `  NN     msg` lines open a numbered violation
- subsequent non-numbered lines concatenate onto the open violation
- table separators (------) don't pollute descriptions
- when nothing parses, the tail of unmatched lines is preserved (not the head)
- stderr is consulted as a fallback when stdout parses to nothing
- passthrough mode (`output: passthrough`) skips structured parsing
"""

import subprocess
from unittest.mock import patch

from bully import (
    Rule,
    _combine_streams,
    _tail_for_description,
    execute_script_rule,
    parse_script_output,
)

# --- phpstan-style indented table output ----------------------------------


PHPSTAN_OUTPUT = """Note: Using configuration file /path/phpstan.neon.
Instructions for interpreting errors
---------

Each error has an associated identifier, like `argument.type`
or `return.missing`.

 ------ -----------------------------------------------------------------------
  Line   src/Models/Broken.php
 ------ -----------------------------------------------------------------------
  11     Method Modman\\Models\\Broken::reportable() return type with generic
         class Illuminate\\Database\\Eloquent\\Relations\\MorphTo does not specify
         its types: TRelatedModel, TDeclaringModel
         identifier: missingType.generics
  18     Method Modman\\Models\\Broken::explode() should return string but
         returns int.
         identifier: return.type
 ------ -----------------------------------------------------------------------


 [ERROR] Found 2 errors
"""


def test_phpstan_indented_table_produces_line_numbered_violations():
    vs = parse_script_output("larastan", "error", PHPSTAN_OUTPUT)
    lines = [v.line for v in vs]
    assert 11 in lines
    assert 18 in lines


def test_phpstan_continuation_lines_join_onto_parent_violation():
    vs = parse_script_output("larastan", "error", PHPSTAN_OUTPUT)
    # The line-11 violation should carry the full wrapped message, not
    # just the first line. Check for content from the 2nd and 3rd wrap rows.
    v11 = next(v for v in vs if v.line == 11)
    assert "MorphTo" in v11.description
    assert "TRelatedModel" in v11.description
    # The next numbered row must NOT leak into this one.
    assert "explode" not in v11.description


def test_phpstan_table_separators_are_dropped():
    vs = parse_script_output("larastan", "error", PHPSTAN_OUTPUT)
    for v in vs:
        assert "------" not in v.description


def test_phpstan_preamble_does_not_pollute_violations():
    vs = parse_script_output("larastan", "error", PHPSTAN_OUTPUT)
    # The "Instructions for interpreting errors" preamble must not attach
    # to any numbered violation.
    for v in vs:
        assert "Instructions for interpreting" not in v.description
        assert "Using configuration file" not in v.description


# --- pest-style wrapped failure -------------------------------------------


PEST_ARCH_OUTPUT = """   FAIL  Tests\\Arch\\ArchTest
  ✗ it does not use debug statements
  Expected src/Foo.php to not use dd() but it does.
  Consider removing the debug statement before committing.

  ✗ it uses final on graders
  src/Graders/Foo.php is not final.
"""


def test_pest_unnumbered_failures_fall_to_tail_fallback():
    # Pest arch failures don't carry line numbers in their compact output.
    # They should surface as tail-preserved violations rather than being
    # silently dropped or collapsed into the rule description alone.
    vs = parse_script_output("pest-arch", "error", PEST_ARCH_OUTPUT)
    assert vs, "expected at least one violation from pest arch output"
    joined = " ".join(v.description for v in vs)
    assert "dd()" in joined or "not final" in joined


# --- tail-not-head preservation -------------------------------------------


def test_unmatched_fallback_preserves_tail_not_head():
    # Long preamble (30 lines) then a short real error at the end. Old
    # behavior: 500-char head join swallows the preamble and drops the
    # error. New behavior: tail preserved.
    preamble = "\n".join(f"preamble line {i} explaining how to read errors" for i in range(30))
    output = f"{preamble}\nreal error: something exploded"
    vs = parse_script_output("r", "error", output)
    joined = " ".join(v.description for v in vs)
    assert "real error" in joined
    assert "exploded" in joined


def test_unmatched_fallback_caps_violation_count():
    # 50 unmatched lines should cap out at the fallback max (20) so the
    # hook output stays bounded.
    output = "\n".join(f"weird garbage line {i}" for i in range(50))
    vs = parse_script_output("r", "error", output)
    assert len(vs) <= 20
    # And the *last* line must be present -- tail preservation.
    joined = " ".join(v.description for v in vs)
    assert "weird garbage line 49" in joined


def test_each_violation_description_capped_not_aggregate():
    # One long unmatched line should be capped, but not concatenated with
    # unrelated following lines.
    long = "x" * 2000
    output = f"{long}\nanother unrelated line"
    vs = parse_script_output("r", "error", output)
    assert len(vs) == 2
    assert len(vs[0].description) <= 500
    assert vs[1].description == "another unrelated line"


# --- stderr fallback in execute_script_rule -------------------------------


def _mock_completed(returncode, stdout="", stderr=""):
    """Build a CompletedProcess shape that subprocess.run returns."""
    return subprocess.CompletedProcess(
        args="cmd", returncode=returncode, stdout=stdout, stderr=stderr
    )


def _script_rule(output_mode="parsed"):
    return Rule(
        id="r",
        description="rule desc",
        engine="script",
        scope=("*",),
        severity="error",
        script="some-tool {file}",
        output_mode=output_mode,
    )


def test_execute_script_rule_consults_stderr_when_stdout_is_empty():
    # Pint writes its FAIL summary to stderr on some setups; stdout is
    # empty. The old pipeline would fall back to rule.description alone.
    rule = _script_rule()
    with patch("bully.engines.script.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed(
            returncode=1,
            stdout="",
            stderr="src/Foo.php:10:5: FAIL missing trailing newline",
        )
        vs = execute_script_rule(rule, "src/Foo.php", "")
    assert len(vs) == 1
    assert vs[0].line == 10
    assert "missing trailing newline" in vs[0].description


def test_execute_script_rule_includes_stderr_tail_in_fallback_description():
    # Neither stdout nor stderr parses to anything structured, but both
    # have content. The fallback description should include a tail of
    # combined output (not just the static rule description).
    rule = _script_rule()
    with patch("bully.engines.script.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed(
            returncode=1,
            stdout="banner line\n==========\n",
            stderr="real failure at the end\n",
        )
        vs = execute_script_rule(rule, "src/Foo.php", "")
    assert len(vs) == 1
    assert "real failure at the end" in vs[0].description
    # Separator rows should not leak into the fallback description.
    assert "====" not in vs[0].description


def test_execute_script_rule_passthrough_skips_parsing():
    # output: passthrough must bypass parse_script_output entirely and
    # emit one violation carrying the tool tail.
    rule = _script_rule(output_mode="passthrough")
    with patch("bully.engines.script.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed(
            returncode=1,
            stdout="src/Foo.php:10:5: this would normally parse\n",
            stderr="extra context\n",
        )
        vs = execute_script_rule(rule, "src/Foo.php", "")
    assert len(vs) == 1
    # Passthrough keeps line=None (no structured parsing).
    assert vs[0].line is None
    # Content includes both stdout and stderr.
    assert "this would normally parse" in vs[0].description
    assert "extra context" in vs[0].description


# --- helpers --------------------------------------------------------------


def test_combine_streams_joins_both_when_both_present():
    combined = _combine_streams("out-text", "err-text")
    assert "out-text" in combined
    assert "err-text" in combined


def test_combine_streams_single_stream():
    assert _combine_streams("only-out", "") == "only-out"
    assert _combine_streams("", "only-err") == "only-err"
    assert _combine_streams("", "") == ""


def test_tail_for_description_drops_separators_and_blanks():
    out = "------\n\nreal line\n===\n"
    assert _tail_for_description(out) == "real line"


def test_tail_for_description_caps_length():
    long_line = "a" * 2000
    assert len(_tail_for_description(long_line)) <= 500
