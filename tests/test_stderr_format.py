"""Golden-file test for _format_blocked_stderr.

To regenerate goldens (if you intentionally changed the format):

    AGENTIC_LINT_REGEN_GOLDENS=1 python3 -m pytest \
        pipeline/tests/test_stderr_format.py -q

The test exits with a pass and rewrites the golden file in place.
"""

import os
from pathlib import Path

from bully import _format_blocked_stderr

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden" / "blocked_stderr"
GOLDEN_FILE = GOLDEN_DIR / "canonical.txt"


CANONICAL_INPUT = {
    "status": "blocked",
    "file": "src/foo.php",
    "violations": [
        {
            "rule": "no-compact",
            "engine": "script",
            "severity": "error",
            "line": 12,
            "description": "compact() is forbidden",
            "suggestion": None,
        },
        {
            "rule": "no-db-facade",
            "engine": "script",
            "severity": "error",
            "line": 27,
            "description": "use Model::query() instead of DB::",
            "suggestion": "replace with Model::query()",
        },
        {
            "rule": "no-missing-line",
            "engine": "script",
            "severity": "warning",
            "line": None,
            "description": "no line info",
        },
    ],
    "passed": ["pint-formatting", "no-env"],
}


def test_blocked_stderr_matches_golden():
    actual = _format_blocked_stderr(CANONICAL_INPUT)
    if os.environ.get("AGENTIC_LINT_REGEN_GOLDENS") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        GOLDEN_FILE.write_text(actual)
    expected = GOLDEN_FILE.read_text()
    assert actual == expected, (
        f"stderr format drifted from golden.\n"
        f"--- golden\n{expected}\n--- actual\n{actual}\n"
        f"If the change is intentional, run with AGENTIC_LINT_REGEN_GOLDENS=1."
    )


def test_golden_file_exists():
    assert GOLDEN_FILE.is_file(), (
        f"missing golden file {GOLDEN_FILE}. Run with AGENTIC_LINT_REGEN_GOLDENS=1 to create it."
    )


def test_stderr_ends_with_newline():
    out = _format_blocked_stderr(CANONICAL_INPUT)
    assert out.endswith("\n")


def test_stderr_includes_all_rule_ids():
    out = _format_blocked_stderr(CANONICAL_INPUT)
    for v in CANONICAL_INPUT["violations"]:
        assert v["rule"] in out


def test_stderr_includes_suggestion_line_when_present():
    out = _format_blocked_stderr(CANONICAL_INPUT)
    assert "suggestion: replace with Model::query()" in out


def test_stderr_includes_passed_checks_trailer():
    out = _format_blocked_stderr(CANONICAL_INPUT)
    assert "Passed checks: pint-formatting, no-env" in out


def test_stderr_no_passed_trailer_when_no_passes():
    result = {
        "status": "blocked",
        "violations": [
            {
                "rule": "r",
                "engine": "script",
                "severity": "error",
                "line": 1,
                "description": "x",
            }
        ],
        "passed": [],
    }
    out = _format_blocked_stderr(result)
    assert "Passed checks" not in out
