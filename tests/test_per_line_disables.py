"""Tests for `# bully-disable: rule-id` per-line suppressions."""

from bully import _line_has_disable, _parse_disable_directive, run_pipeline

RULE_YAML = (
    "rules:\n"
    "  no-foo:\n"
    '    description: "forbidden"\n'
    "    engine: script\n"
    '    scope: "*.py"\n'
    "    severity: error\n"
    "    script: \"grep -n 'FORBIDDEN' {file} && exit 1 || exit 0\"\n"
    "  no-bar:\n"
    '    description: "also forbidden"\n'
    "    engine: script\n"
    '    scope: "*.py"\n'
    "    severity: error\n"
    "    script: \"grep -n 'FORBIDDEN' {file} && exit 1 || exit 0\"\n"
)


# ---------------------------------------------------------------------------
# Directive parser
# ---------------------------------------------------------------------------


def test_parse_single_rule_directive():
    ids, reason = _parse_disable_directive("# bully-disable: no-foo because reason")
    assert ids == {"no-foo"}
    assert reason == "because reason"


def test_parse_directive_captures_first_id_and_rest_as_reason():
    # Current parser: the first whitespace-delimited token after the colon is
    # the id; the rest is the reason. Commas in the reason are preserved.
    ids, reason = _parse_disable_directive("# bully-disable: no-foo, no-bar explanation")
    assert ids == {"no-foo"}
    assert reason is not None and "no-bar" in reason


def test_parse_disable_all_when_no_ids():
    # empty id list means disable everything
    ids, _ = _parse_disable_directive("# bully-disable")
    assert ids == set()


def test_parse_non_directive_returns_none():
    ids, reason = _parse_disable_directive("# just a plain comment")
    assert ids is None
    assert reason is None


# ---------------------------------------------------------------------------
# Comment-prefix detection on the actual file
# ---------------------------------------------------------------------------


def test_same_line_hash_comment_suppresses(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "a.py"
    target.write_text("x = 'FORBIDDEN'  # bully-disable: no-foo legacy\n")

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    # Only no-bar fires; no-foo is suppressed
    assert result["status"] == "blocked"
    rule_ids = {v["rule"] for v in result["violations"]}
    assert "no-foo" not in rule_ids
    assert "no-bar" in rule_ids


def test_previous_line_comment_suppresses(tmp_path):
    # Use a single-rule config so the previous-line directive covers the
    # lone violating line.
    single_rule_yaml = (
        "rules:\n"
        "  no-foo:\n"
        '    description: "forbidden"\n'
        "    engine: script\n"
        '    scope: "*.py"\n'
        "    severity: error\n"
        "    script: \"grep -n 'FORBIDDEN' {file} && exit 1 || exit 0\"\n"
    )
    (tmp_path / ".bully.yml").write_text(single_rule_yaml)
    target = tmp_path / "b.py"
    target.write_text("# bully-disable: no-foo known legacy\nx = 'FORBIDDEN'\n")
    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    # no-foo disabled on line 2 via the previous-line directive -> pass.
    assert result["status"] == "pass"


def test_slash_slash_comment_suppresses():
    assert _line_has_disable_file(
        "x = y;  // bully-disable: no-foo\n",
        line=1,
        rule_id="no-foo",
    )


def test_dash_dash_comment_suppresses():
    # SQL/Lua/Haskell-style
    assert _line_has_disable_file(
        "SELECT 1; -- bully-disable: no-foo\n",
        line=1,
        rule_id="no-foo",
    )


def test_block_comment_suppresses():
    assert _line_has_disable_file(
        "/* bully-disable: no-foo */ x = 1;\n",
        line=1,
        rule_id="no-foo",
    )


def test_non_matching_rule_id_not_suppressed():
    assert not _line_has_disable_file(
        "x = 1  # bully-disable: some-other-rule\n",
        line=1,
        rule_id="no-foo",
    )


def test_disable_without_ids_suppresses_all():
    assert _line_has_disable_file(
        "x = 1  # bully-disable\n",
        line=1,
        rule_id="any-rule-at-all",
    )


def _line_has_disable_file(contents: str, line: int, rule_id: str) -> bool:
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(contents)
        p = f.name
    return _line_has_disable(p, line, rule_id)
