"""Tests for pipeline-side can't-match filters (plan 4.2)."""

from bully import Rule, _can_match_diff, _rule_add_perspective


def _rule(desc: str = "avoid the bad pattern") -> Rule:
    return Rule(
        id="r",
        description=desc,
        engine="semantic",
        scope=("*",),
        severity="error",
    )


def test_empty_diff_skipped():
    ok, reason = _can_match_diff(_rule(), "")
    assert ok is False
    assert reason == "empty-diff"


def test_whitespace_only_additions_skipped():
    diff = "@@ -1,2 +1,4 @@\n+   \n+\t\n"
    ok, reason = _can_match_diff(_rule(), diff)
    assert ok is False
    assert reason == "whitespace-only-additions"


def test_comment_only_additions_skip_non_comment_rule():
    diff = "@@ -1,2 +1,4 @@\n+// a comment\n+// another comment\n"
    ok, reason = _can_match_diff(_rule("avoid bad variable names"), diff)
    assert ok is False
    assert reason == "comment-only-additions"


def test_comment_only_additions_kept_for_comment_rule():
    diff = "@@ -1,2 +1,4 @@\n+// a comment\n+// another comment\n"
    r = _rule("comment quality must be professional")
    ok, _ = _can_match_diff(r, diff)
    assert ok is True


def test_pure_deletion_skips_avoid_rule():
    diff = "@@ -1,2 +0,0 @@\n-old line 1\n-old line 2\n"
    ok, reason = _can_match_diff(_rule("avoid X"), diff)
    assert ok is False
    assert reason == "pure-deletion-add-perspective-rule"


def test_pure_deletion_kept_for_refactor_rule():
    diff = "@@ -1,2 +0,0 @@\n-old line 1\n-old line 2\n"
    # no avoid/no/ban/don't in description
    r = _rule("extract helper methods when appropriate")
    ok, _ = _can_match_diff(r, diff)
    assert ok is True


def test_single_added_line_is_dispatched():
    """One-line additions are the common case for real semantic violations
    (e.g. introducing `eval(input)`) -- the gate must let them through."""
    diff = "@@ -5,1 +5,2 @@\n+result = eval(user_input)\n"
    ok, reason = _can_match_diff(_rule(), diff)
    assert ok is True, f"expected dispatch, got skip reason {reason!r}"


def test_single_added_line_with_removal_is_dispatched():
    """Mixed one-line edit (one removal, one addition) also passes."""
    diff = "@@ -5,1 +5,1 @@\n-old_safe_call()\n+result = eval(user_input)\n"
    ok, reason = _can_match_diff(_rule(), diff)
    assert ok is True, f"expected dispatch, got skip reason {reason!r}"


def test_multi_line_added_passes():
    diff = "@@ -5,1 +5,3 @@\n+added line one\n+added line two\n"
    ok, _ = _can_match_diff(_rule(), diff)
    assert ok is True


def test_single_comment_line_still_skipped():
    """The comment-only filter still has teeth on one-line edits."""
    diff = "@@ -5,1 +5,2 @@\n+# just a single comment\n"
    ok, reason = _can_match_diff(_rule("avoid bad names"), diff)
    assert ok is False
    assert reason == "comment-only-additions"


def test_single_whitespace_line_still_skipped():
    """The whitespace-only filter still has teeth on one-line edits."""
    diff = "@@ -5,1 +5,2 @@\n+   \n"
    ok, reason = _can_match_diff(_rule(), diff)
    assert ok is False
    assert reason == "whitespace-only-additions"


def test_hash_comment_only_additions_skipped():
    diff = "@@ -1,2 +1,4 @@\n+# hash comment 1\n+# hash comment 2\n"
    ok, reason = _can_match_diff(_rule("avoid bad names"), diff)
    assert ok is False
    assert reason == "comment-only-additions"


def test_sql_dash_dash_comment_only_additions_skipped():
    diff = "@@ -1,2 +1,4 @@\n+-- SQL comment\n+-- another comment\n"
    ok, reason = _can_match_diff(_rule("avoid joins"), diff)
    assert ok is False
    assert reason == "comment-only-additions"


# ---------------------------------------------------------------------------
# Word-boundary matching for `_rule_add_perspective` (FT2).
#
# Substring matching used to false-flag descriptions that merely contained a
# trigger word as a sub-token ("banner" -> "ban", "avoidance" -> "avoid",
# "no-op" -> "no-"). With Task 4 lowering the floor for single-line edits,
# these false positives now actively skip diffs that should be evaluated.
# ---------------------------------------------------------------------------


def test_rule_add_perspective_rejects_substring_no_op():
    assert _rule_add_perspective("no-op pattern detection") is False


def test_rule_add_perspective_rejects_substring_banner():
    assert _rule_add_perspective("banner placement") is False


def test_rule_add_perspective_rejects_substring_avoidance():
    assert _rule_add_perspective("avoidance count") is False


def test_rule_add_perspective_rejects_substring_avoidant():
    assert _rule_add_perspective("avoidant rule") is False


def test_rule_add_perspective_matches_no_as_word():
    assert _rule_add_perspective("no global state") is True


def test_rule_add_perspective_matches_ban_as_word():
    assert _rule_add_perspective("ban inline scripts") is True


def test_rule_add_perspective_matches_avoid_as_word():
    assert _rule_add_perspective("avoid eval") is True


def test_rule_add_perspective_matches_dont_with_apostrophe():
    assert _rule_add_perspective("don't use console.log") is True


def test_rule_add_perspective_is_case_insensitive():
    # Descriptions are user-authored; uppercase variants must still match.
    assert _rule_add_perspective("AVOID eval") is True
    assert _rule_add_perspective("Don't use console.log") is True


def test_can_match_diff_does_not_skip_no_op_rule_on_pure_deletion():
    """End-to-end: a rule whose description merely contains "no-" as a
    substring (e.g. "no-op detector") should NOT be filtered by the
    pure-deletion-add-perspective gate.
    """
    diff = "@@ -1,2 +0,0 @@\n-old line 1\n-old line 2\n"
    ok, reason = _can_match_diff(_rule("no-op detector"), diff)
    assert ok is True, f"expected dispatch, got skip reason {reason!r}"


def test_can_match_diff_still_skips_avoid_rule_on_pure_deletion():
    """End-to-end: a real "avoid X" rule must still be filtered by the
    pure-deletion-add-perspective gate after the regex tightening.
    """
    diff = "@@ -1,2 +0,0 @@\n-old line 1\n-old line 2\n"
    ok, reason = _can_match_diff(_rule("avoid eval"), diff)
    assert ok is False
    assert reason == "pure-deletion-add-perspective-rule"
