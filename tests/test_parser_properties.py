"""Hypothesis property tests for parse_config.

Invariants tested:
- parse_config never raises anything other than ConfigError or returns list[Rule]
- valid generated configs round-trip through serialize -> parse
- ConfigError.line is always 1-indexed and in range
- duplicate rule ids are always flagged
- tab indentation is always rejected
- unknown rule fields are always rejected
"""

import re
import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bully import ConfigError, Rule, parse_config

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A restricted alphabet that can plausibly exercise the parser without
# running into YAML features we deliberately do not support.
CHARS = st.text(
    alphabet=st.sampled_from(
        list(string.ascii_letters) + list(string.digits) + [" ", ":", "-", "#", '"', "'", "\n"]
    ),
    max_size=80,
)


_ID_CHARS = string.ascii_lowercase + string.digits + "-"


@st.composite
def rule_id_strategy(draw):
    n = draw(st.integers(min_value=1, max_value=20))
    first = draw(st.sampled_from(string.ascii_lowercase))
    rest = draw(st.text(alphabet=_ID_CHARS, min_size=0, max_size=n))
    return first + rest


def _safe_desc_text() -> st.SearchStrategy[str]:
    """Description text that is safe to embed in double quotes."""
    return st.text(
        alphabet=st.sampled_from(list(string.ascii_letters + string.digits) + [" ", ",", "."]),
        min_size=1,
        max_size=30,
    )


@st.composite
def scope_glob(draw):
    ext = draw(st.sampled_from(["py", "ts", "js", "php", "go", "rs", "rb"]))
    return f"*.{ext}"


@st.composite
def rule_strategy(draw):
    rid = draw(rule_id_strategy())
    engine = draw(st.sampled_from(["script", "semantic"]))
    severity = draw(st.sampled_from(["error", "warning"]))
    scope = draw(st.lists(scope_glob(), min_size=1, max_size=3, unique=True))
    desc = draw(_safe_desc_text())
    script = "exit 0" if engine == "script" else None
    return (rid, engine, severity, tuple(scope), desc, script)


def _serialize(rules) -> str:
    lines = ["rules:"]
    for rid, engine, sev, scope, desc, script in rules:
        lines.append(f"  {rid}:")
        lines.append(f'    description: "{desc}"')
        lines.append(f"    engine: {engine}")
        if len(scope) == 1:
            lines.append(f'    scope: "{scope[0]}"')
        else:
            quoted = ", ".join(f'"{s}"' for s in scope)
            lines.append(f"    scope: [{quoted}]")
        lines.append(f"    severity: {sev}")
        if script is not None:
            lines.append(f'    script: "{script}"')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 1. No crash on arbitrary input
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(blob=CHARS)
def test_never_raises_unexpected_exception(tmp_path_factory, blob):
    path = tmp_path_factory.mktemp("fuzz") / "config.yml"
    path.write_text(blob)
    try:
        result = parse_config(str(path))
    except ConfigError:
        return
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"parse_config raised {type(e).__name__}, expected ConfigError: {e}"
        ) from e
    # If it parsed, must be a list of Rule
    assert isinstance(result, list)
    assert all(isinstance(r, Rule) for r in result)


# ---------------------------------------------------------------------------
# 2. Round-trip valid configs
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rules=st.lists(
        rule_strategy(),
        min_size=1,
        max_size=6,
        unique_by=lambda r: r[0],
    ),
)
def test_round_trip_valid_config(tmp_path_factory, rules):
    text = _serialize(rules)
    path = tmp_path_factory.mktemp("rt") / "config.yml"
    path.write_text(text)

    parsed = parse_config(str(path))
    assert len(parsed) == len(rules)
    parsed_by_id = {r.id: r for r in parsed}
    for rid, engine, severity, _scope, _desc, _script in rules:
        assert rid in parsed_by_id, f"rule id {rid} lost in round-trip"
        r = parsed_by_id[rid]
        assert r.engine == engine
        assert r.severity == severity


# ---------------------------------------------------------------------------
# 3. Line numbers are always 1-indexed and in range when ConfigError fires
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(blob=CHARS)
def test_config_error_line_in_range(tmp_path_factory, blob):
    path = tmp_path_factory.mktemp("line") / "config.yml"
    path.write_text(blob)
    try:
        parse_config(str(path))
    except ConfigError as e:
        if e.line is not None:
            n_lines = len(blob.splitlines()) or 1
            assert 1 <= e.line <= n_lines, (
                f"ConfigError.line={e.line} not in 1..{n_lines} for input {blob!r}"
            )


# ---------------------------------------------------------------------------
# 4. Duplicate ids are always caught
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rid=rule_id_strategy())
def test_duplicate_ids_detected(tmp_path_factory, rid):
    text = (
        "rules:\n"
        f"  {rid}:\n"
        '    description: "first"\n'
        "    engine: script\n"
        '    scope: "*.py"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
        f"  {rid}:\n"
        '    description: "second"\n'
        "    engine: script\n"
        '    scope: "*.py"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
    )
    path = tmp_path_factory.mktemp("dup") / "config.yml"
    path.write_text(text)
    with pytest.raises(ConfigError) as exc_info:
        parse_config(str(path))
    assert "duplicate" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 5. Tab indentation always detected
# ---------------------------------------------------------------------------


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rid=rule_id_strategy())
def test_tab_indentation_detected(tmp_path_factory, rid):
    # Put a tab into the rule-id line indentation.
    text = (
        "rules:\n"
        f"\t{rid}:\n"
        '    description: "x"\n'
        "    engine: script\n"
        '    scope: "*.py"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
    )
    path = tmp_path_factory.mktemp("tab") / "config.yml"
    path.write_text(text)
    with pytest.raises(ConfigError) as exc_info:
        parse_config(str(path))
    assert "tab" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 6. Unknown rule fields are always rejected
# ---------------------------------------------------------------------------


# Match the rule-field regex for valid field names (avoid coincidentally
# generating one of them).
_VALID_FIELDS = {"description", "engine", "scope", "severity", "script", "fix_hint"}


@st.composite
def unknown_field_name(draw):
    name = draw(
        st.text(
            alphabet=st.sampled_from(string.ascii_lowercase + "_"),
            min_size=3,
            max_size=15,
        )
    )
    # Reject valid ones so the test actually exercises the unknown-field path.
    if name in _VALID_FIELDS or not re.match(r"^[a-z_][a-z_]*$", name):
        name = f"{name}_x"
    return name


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rid=rule_id_strategy(), bad_field=unknown_field_name())
def test_unknown_rule_field_rejected(tmp_path_factory, rid, bad_field):
    if bad_field in _VALID_FIELDS:
        return  # skip: can't reliably avoid collision for tiny strings
    text = (
        "rules:\n"
        f"  {rid}:\n"
        '    description: "x"\n'
        "    engine: script\n"
        '    scope: "*.py"\n'
        "    severity: error\n"
        '    script: "exit 0"\n'
        f"    {bad_field}: something\n"
    )
    path = tmp_path_factory.mktemp("uf") / "config.yml"
    path.write_text(text)
    with pytest.raises(ConfigError) as exc_info:
        parse_config(str(path))
    assert bad_field in str(exc_info.value)
