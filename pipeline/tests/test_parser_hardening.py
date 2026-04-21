"""Tests for hardened YAML parser: list scopes, inline comments, quote handling."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import filter_rules, parse_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_description_with_colon_is_preserved():
    rules = {r.id: r for r in parse_config(str(FIXTURES / "tricky-config.yml"))}
    assert rules["quoted-colon"].description == "Rule: with a colon in description"


def test_inline_comment_is_stripped():
    rules = {r.id: r for r in parse_config(str(FIXTURES / "tricky-config.yml"))}
    assert "#" not in rules["quoted-colon"].description
    assert "inline" not in rules["quoted-colon"].description


def test_script_with_embedded_colons_parses():
    rules = {r.id: r for r in parse_config(str(FIXTURES / "tricky-config.yml"))}
    assert rules["quoted-colon"].script == "grep -n 'foo: bar' {file} && exit 1 || exit 0"


def test_list_scope_parses_as_tuple():
    rules = {r.id: r for r in parse_config(str(FIXTURES / "tricky-config.yml"))}
    assert rules["multi-scope"].scope == ("*.php", "*.ts", "*.js")


def test_list_scope_matches_any_glob():
    rules = parse_config(str(FIXTURES / "tricky-config.yml"))
    multi = [r for r in rules if r.id == "multi-scope"]
    assert len(filter_rules(multi, "src/foo.php")) == 1
    assert len(filter_rules(multi, "src/foo.ts")) == 1
    assert len(filter_rules(multi, "src/foo.js")) == 1
    assert len(filter_rules(multi, "src/foo.rb")) == 0


def test_single_scope_still_works_as_before():
    rules = {r.id: r for r in parse_config(str(FIXTURES / "tricky-config.yml"))}
    assert rules["unquoted-value"].scope == ("*",)


def test_unquoted_description_parses():
    rules = {r.id: r for r in parse_config(str(FIXTURES / "tricky-config.yml"))}
    assert rules["unquoted-value"].description == "Plain unquoted description"


def test_single_quoted_value_with_embedded_doubles():
    rules = {r.id: r for r in parse_config(str(FIXTURES / "tricky-config.yml"))}
    assert rules["mixed-quotes"].description == 'Single quoted with "embedded doubles"'


def test_mismatched_outer_quotes_not_stripped():
    # If only one side has a quote, don't strip it -- avoids eating legitimate characters.
    # We test this via the parser helper so we don't need a fixture file.
    from pipeline import _parse_scalar

    assert _parse_scalar('"foo') == '"foo'
    assert _parse_scalar('foo"') == 'foo"'
    assert _parse_scalar('"foo"') == "foo"
    assert _parse_scalar("'foo'") == "foo"


# ---------------------------------------------------------------------------
# Regression tests: YAML escape sequence handling in double-quoted scalars.
# The silent miscompile case: `"\\."` must yield `\.` (regex-correct), not `\\.`.
# ---------------------------------------------------------------------------


def test_double_quoted_backslash_escape_collapses_to_single_backslash():
    """`"\\\\."` in YAML must produce `\\.` -- the escape grep regex needs."""
    from pipeline import _parse_scalar

    # Raw input from YAML parser (outer quotes + inner double-backslash + dot).
    assert _parse_scalar(r'"\\."') == r"\."


def test_double_quoted_escapes_for_grep_console_log_pattern():
    r"""The real-world console.log regex pattern must round-trip correctly.

    YAML-quoted:  "(^|[^a-zA-Z_.])console\\.log\\("
    Should become: (^|[^a-zA-Z_.])console\.log\(   (what grep -E expects)
    """
    from pipeline import _parse_scalar

    raw = r'"(^|[^a-zA-Z_.])console\\.log\\("'
    assert _parse_scalar(raw) == r"(^|[^a-zA-Z_.])console\.log\("


def test_double_quoted_escape_embedded_double_quote():
    """`"foo\\"bar"` should yield `foo"bar`."""
    from pipeline import _parse_scalar

    # raw bytes in the YAML file: "foo\"bar"
    assert _parse_scalar(r'"foo\"bar"') == 'foo"bar'


def test_double_quoted_escape_newline_tab_return():
    """Standard C-style escapes are supported in double-quoted scalars."""
    from pipeline import _parse_scalar

    assert _parse_scalar(r'"a\nb"') == "a\nb"
    assert _parse_scalar(r'"a\tb"') == "a\tb"
    assert _parse_scalar(r'"a\rb"') == "a\rb"
    assert _parse_scalar(r'"a\0b"') == "a\x00b"
    assert _parse_scalar(r'"a\/b"') == "a/b"


def test_double_quoted_unknown_escape_kept_literally():
    """Unknown escape sequences are kept verbatim (graceful, no raise)."""
    from pipeline import _parse_scalar

    # `\z` isn't a standard escape -- keep as `\z` rather than erroring.
    assert _parse_scalar(r'"a\zb"') == r"a\zb"


def test_single_quoted_no_backslash_processing():
    """Single-quoted scalars keep backslashes literally; only `''` is an escape."""
    from pipeline import _parse_scalar

    assert _parse_scalar(r"'\\.'") == r"\\."
    assert _parse_scalar(r"'a\nb'") == r"a\nb"


def test_single_quoted_double_single_quote_escape():
    """In single-quoted YAML scalars, `''` -> `'`."""
    from pipeline import _parse_scalar

    # YAML spec: 'can''t' -> can't
    assert _parse_scalar("'can''t'") == "can't"


def test_plain_unquoted_scalar_not_touched():
    """Unquoted scalars must not get escape processing."""
    from pipeline import _parse_scalar

    assert _parse_scalar(r"a\nb") == r"a\nb"
    assert _parse_scalar(r"foo\\bar") == r"foo\\bar"


# ---------------------------------------------------------------------------
# Recursive `**` glob matching in scope filters.
# Python's `PurePath.match` only grew recursive-`**` support in 3.13; bully
# supports 3.10+, so we implement the matcher ourselves.
# ---------------------------------------------------------------------------


def _scope_rule(scope):
    from pipeline import Rule

    return Rule(id="r", description="d", engine="semantic", scope=scope, severity="error")


def test_double_star_matches_zero_directories():
    """`src/**/*.ts` must match `src/foo.ts` (zero intermediate dirs)."""
    from pipeline import filter_rules

    rules = [_scope_rule(("src/**/*.ts",))]
    assert len(filter_rules(rules, "src/foo.ts")) == 1


def test_double_star_matches_one_directory():
    """`src/**/*.ts` must match `src/sub/foo.ts`."""
    from pipeline import filter_rules

    rules = [_scope_rule(("src/**/*.ts",))]
    assert len(filter_rules(rules, "src/sub/foo.ts")) == 1


def test_double_star_matches_many_directories():
    """`src/**/*.ts` must match arbitrarily nested paths."""
    from pipeline import filter_rules

    rules = [_scope_rule(("src/**/*.ts",))]
    assert len(filter_rules(rules, "src/a/b/c/d/foo.ts")) == 1


def test_double_star_does_not_match_sibling_trees():
    """`src/**/*.ts` must not match `lib/foo.ts` -- prefix still anchors."""
    from pipeline import filter_rules

    rules = [_scope_rule(("src/**/*.ts",))]
    assert len(filter_rules(rules, "lib/foo.ts")) == 0


def test_leading_double_star_matches_root_file():
    """`**/*.ts` must match a top-level `foo.ts` (zero dirs above)."""
    from pipeline import filter_rules

    rules = [_scope_rule(("**/*.ts",))]
    assert len(filter_rules(rules, "foo.ts")) == 1


def test_leading_double_star_matches_nested_file():
    """`**/*.ts` must match deeply nested `a/b/foo.ts`."""
    from pipeline import filter_rules

    rules = [_scope_rule(("**/*.ts",))]
    assert len(filter_rules(rules, "a/b/c/foo.ts")) == 1


def test_plain_glob_still_matches_basename():
    """Simple `*.ts` must still work (basename match, the common case)."""
    from pipeline import filter_rules

    rules = [_scope_rule(("*.ts",))]
    assert len(filter_rules(rules, "anywhere/foo.ts")) == 1
    assert len(filter_rules(rules, "foo.ts")) == 1
    assert len(filter_rules(rules, "foo.js")) == 0


def test_trailing_double_star_matches_all_descendants():
    """`tests/**` must match everything below `tests/`."""
    from pipeline import filter_rules

    rules = [_scope_rule(("tests/**",))]
    assert len(filter_rules(rules, "tests/foo.py")) == 1
    assert len(filter_rules(rules, "tests/a/b/c.py")) == 1
    assert len(filter_rules(rules, "src/foo.py")) == 0
