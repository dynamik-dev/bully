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
