"""Regression: 0.4.0 scope matcher fails on absolute paths with `**` patterns.

The hook passes absolute file paths (Claude Code's PostToolUse payload carries
them). Before 0.4.0, `filter_rules` used `PurePath.match`, which is
right-anchored and therefore matched `"app/**/*.php"` against
`"/Users/.../proj/app/Posts/Post.php"`.

0.4.0's `edb362f` replaced that with a hand-rolled `_scope_glob_matches` that
anchors the first segment at `parts[0]`. For absolute paths, `parts[0]` is
`"/"`, so every `**` pattern misses. The visible symptom in a user's
`.bully/log.jsonl` is `rules: []` on every entry — no script / AST / semantic
rule ever runs, even though the hook is wired up and telemetry is written.

Fixtures:
    - ``fixtures/groups4.bully.yml`` — copy of a real-world Laravel + Inertia
      config from the Groups4 project where this was first observed.
    - ``fixtures/groups4-0.4.0-log.jsonl`` — 50-line tail of the same
      project's telemetry from the 0.4.0 window. Every entry has ``rules: []``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import _scope_glob_matches, filter_rules, parse_config

FIXTURES = Path(__file__).parent / "fixtures"
GROUPS4_CONFIG = FIXTURES / "groups4.bully.yml"
GROUPS4_LOG = FIXTURES / "groups4-0.4.0-log.jsonl"

# Representative absolute paths pulled from the real Groups4 log.
ABS_PREFIX = "/Users/chrisarter/Documents/projects/Groups4"


def test_scope_glob_matches_absolute_path_with_double_star():
    """The root cause: `_scope_glob_matches` with `**` fails on absolute paths."""
    rel = "app/Posts/Models/Post.php"
    abs_path = f"{ABS_PREFIX}/{rel}"

    assert _scope_glob_matches("app/**/*.php", rel) is True, (
        "relative path still matches (was working in 0.3.x)"
    )
    assert _scope_glob_matches("app/**/*.php", abs_path) is True, (
        "absolute path must match too -- the hook always passes absolute paths"
    )


def test_scope_glob_non_double_star_handles_absolute_paths():
    """Sanity: non-`**` patterns use PurePath.match and survive absolute paths.

    This is why the regression is not total -- rules whose scope has no `**`
    (like ``tests/Pest.php``) still fire.
    """
    assert _scope_glob_matches("tests/Pest.php", f"{ABS_PREFIX}/tests/Pest.php") is True


def test_filter_rules_matches_real_groups4_paths():
    """End-to-end: parse the real config and filter absolute paths from the log."""
    rules = parse_config(str(GROUPS4_CONFIG))

    expectations: dict[str, set[str]] = {
        # PHP model under app/ — should hit all the app/** PHP rules.
        f"{ABS_PREFIX}/app/Posts/Models/Post.php": {
            "strict-types",
            "no-compact",
            "no-db-facade",
            "no-event-helper",
            "full-type-hints",
            "inline-single-use-vars",
            "extract-complex-logic",
            "pint-formatting",
            "no-orchestration-labels",
            "no-json-api-routes",
            "capabilities-not-identity",
            "pest-arch-tests",
        },
        # Action class under app/**/Actions — gate-foruser-in-actions activates.
        f"{ABS_PREFIX}/app/Posts/Actions/PinPost.php": {
            "strict-types",
            "no-compact",
            "no-db-facade",
            "no-event-helper",
            "full-type-hints",
            "inline-single-use-vars",
            "extract-complex-logic",
            "pint-formatting",
            "no-orchestration-labels",
            "no-json-api-routes",
            "capabilities-not-identity",
            "gate-foruser-in-actions",
            "pest-arch-tests",
        },
        # Database migration.
        f"{ABS_PREFIX}/database/migrations/2026_04_21_201212_add_pinned_at_to_posts_table.php": {
            "strict-types",
            "no-compact",
            "full-type-hints",
            "pint-formatting",
        },
        # TSX component. Only `.tsx`-only scopes are expected here — scopes
        # written with brace expansion (e.g. `*.{ts,tsx}`) do not match,
        # but that is a separate latent bug (see
        # `test_brace_expansion_in_scope_is_unsupported` below). It predates
        # the 0.4.0 regression: the real pre-0.4.0 log shows zero hits on
        # these rules from `.tsx` files.
        f"{ABS_PREFIX}/resources/js/components/post-card.tsx": {
            "no-raw-anchor-navigation",
            "prefer-useform",
            "no-index-as-key",
        },
    }

    for abs_path, expected in expectations.items():
        matched_ids = {r.id for r in filter_rules(rules, abs_path)}
        missing = expected - matched_ids
        assert not missing, (
            f"{abs_path}\n  missing rules: {sorted(missing)}\n  got: {sorted(matched_ids)}"
        )


def test_scope_glob_does_not_over_match():
    """The suffix-scan fix must not create false positives.

    `app/**/*.php` should match `/Users/.../proj/app/foo.php` but NOT
    `/some/vendor/notapp/foo.php` or `/some/proj/appetite/foo.php`.
    """
    assert _scope_glob_matches("app/**/*.php", "/x/y/app/foo.php") is True
    assert _scope_glob_matches("app/**/*.php", "/x/y/notapp/foo.php") is False
    assert _scope_glob_matches("app/**/*.php", "/x/y/appetite/foo.php") is False
    # A file named app/foo but the wrong extension stays out.
    assert _scope_glob_matches("app/**/*.php", "/x/app/foo.js") is False


def test_brace_expansion_in_scope_is_unsupported():
    """Document a separate latent gap: `*.{ts,tsx}` is not expanded.

    fnmatch has no brace-expansion support, so a scope of
    `resources/js/**/*.{ts,tsx}` matches literally zero real files. This is
    *not* the 0.4.0 regression -- the pre-0.4.0 log from Groups4 confirms
    these scopes never fired on `.tsx` files either. Tracked here so a fix
    can flip this test to the opposite assertion.
    """
    abs_tsx = f"{ABS_PREFIX}/resources/js/components/post-card.tsx"
    assert _scope_glob_matches("resources/js/**/*.{ts,tsx}", abs_tsx) is False, (
        "if this starts returning True, the brace-expansion gap was closed -- "
        "flip the assertion and delete this note"
    )


def test_real_log_shows_empty_rules_confirms_regression_surface():
    """Every 0.4.0 log entry shows `rules: []`. This fixture pins that fact.

    If a later fix restores scope matching but we re-record logs from
    a working run, those rows will have populated rules. Until then,
    this asserts the shape of the bug as observed in the wild.
    """
    entries = [json.loads(line) for line in GROUPS4_LOG.read_text().splitlines() if line]
    assert entries, "fixture should not be empty"
    # These are all pass/empty — the symptom.
    passes_with_no_rules = [e for e in entries if e.get("status") == "pass" and not e.get("rules")]
    assert len(passes_with_no_rules) == len(entries), (
        "0.4.0 fixture should show the symptom: every pass has rules=[]"
    )
