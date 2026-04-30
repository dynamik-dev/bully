"""Single-file YAML-subset parser for .bully.yml.

Hand-rolled because the runtime is stdlib-only — no PyYAML. Parses the narrow
subset bully needs (top-level keys, indented rule definitions, inline lists,
nested mappings for context/when/require/capabilities) and emits structured
ConfigError with line numbers on malformed input.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_ENGINES = {"script", "semantic", "ast", "session"}
VALID_SEVERITIES = {"error", "warning"}
VALID_RULE_FIELDS = {
    "description",
    "engine",
    "scope",
    "severity",
    "script",
    "fix_hint",
    "pattern",
    "language",
    "output",
    "context",
    "when",
    "require",
    "capabilities",
}
VALID_OUTPUT_MODES = {"parsed", "passthrough"}
VALID_TOP_LEVEL = {"rules", "schema_version", "extends", "skip", "execution"}


class ConfigError(Exception):
    """Raised on malformed config input. Carries a 1-indexed line number."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        self.message = message
        prefix = f"line {line}: " if line is not None else ""
        super().__init__(f"{prefix}{message}")


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    engine: str
    scope: tuple[str, ...]
    severity: str
    script: str | None = None
    fix_hint: str | None = None
    pattern: str | None = None
    language: str | None = None
    output_mode: str = "parsed"
    # PR 1c: per-rule context-include — dict like {"lines": 30}. When set,
    # the dispatcher reads N lines around each diff hunk and surfaces them
    # to the evaluator subagent as `<EXCERPT_FOR_RULE>` inside UNTRUSTED_EVIDENCE.
    context: dict | None = None
    # PR 3: session-engine rules — `when.changed_any` selects the rule into
    # the Stop check; `require.changed_any` declares which paths must also
    # appear in the cumulative changed-set. Both are dicts shaped like
    # `{"changed_any": [glob, ...]}`.
    when: dict | None = None
    require: dict | None = None
    # PR 5: per-rule declarative capability profile applied to script-engine
    # subprocess env. Best-effort, env-based -- not kernel sandboxing.
    capabilities: dict | None = None


@dataclass
class Violation:
    rule: str
    engine: str
    severity: str
    line: int | None
    description: str
    suggestion: str | None = None


# ---------------------------------------------------------------------------
# Scalar/list helpers
# ---------------------------------------------------------------------------


def _strip_inline_comment(raw: str) -> str:
    """Remove a trailing ` # comment` while respecting quoted regions."""
    in_single = False
    in_double = False
    for i, ch in enumerate(raw):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and (i == 0 or raw[i - 1].isspace()):
            return raw[:i].rstrip()
    return raw


# YAML double-quoted escape table (subset per the YAML 1.2 spec plus the few
# C-style escapes we actually see in bully configs). Unknown escapes fall
# through as `\x` (backslash preserved) so unusual regex patterns survive.
_DOUBLE_QUOTED_ESCAPES: dict[str, str] = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "/": "/",
    "0": "\x00",
}


def _unescape_double_quoted(inner: str) -> str:
    """Apply YAML double-quoted escape processing to the inside of a scalar."""
    if "\\" not in inner:
        return inner
    out: list[str] = []
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\" and i + 1 < n:
            nxt = inner[i + 1]
            mapped = _DOUBLE_QUOTED_ESCAPES.get(nxt)
            if mapped is not None:
                out.append(mapped)
            else:
                out.append(ch)
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_scalar(raw: str) -> str:
    """Normalize a scalar value: strip inline comment, then process YAML quote escapes."""
    raw = _strip_inline_comment(raw).strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return _unescape_double_quoted(raw[1:-1])
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _parse_inline_list(raw: str) -> list[str] | None:
    """Parse `[a, b, "c"]` into a list of scalars, or return None if not a list."""
    raw = _strip_inline_comment(raw).strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    inner = raw[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    for ch in inner:
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == "," and not in_single and not in_double:
            items.append(_parse_scalar("".join(buf)))
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append(_parse_scalar("".join(buf)))
    return items


# ---------------------------------------------------------------------------
# parse_config with line-numbered errors
# ---------------------------------------------------------------------------


@dataclass
class ParsedConfig:
    """Internal structure returned by parse_single_file."""

    rules: list[Rule] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    schema_version: int | None = None
    max_workers: int | None = None


def _normalize_scope(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    if value is None:
        return ("*",)
    return (str(value),)


def parse_single_file(path: str) -> ParsedConfig:
    """Parse one .bully.yml into ParsedConfig. Raises ConfigError on malformed input."""
    rules: list[Rule] = []
    extends: list[str] = []
    schema_version: int | None = None

    current_id: str | None = None
    current_id_line: int | None = None
    fields: dict[str, object] = {}
    field_lines: dict[str, int] = {}
    folding_key: str | None = None
    folded_lines: list[str] = []

    seen_ids: set[str] = set()
    in_rules_block = False
    in_extends_block = False
    in_skip_block = False
    in_execution_block = False
    in_nested_rule_field: str | None = None
    nested_rule_field_dict: dict[str, object] = {}
    skip: list[str] = []
    max_workers: int | None = None

    def finalize_rule() -> None:
        nonlocal current_id, fields, field_lines
        if current_id is not None:
            if current_id in seen_ids:
                raise ConfigError(f"duplicate rule id '{current_id}'", current_id_line)
            seen_ids.add(current_id)
            rules.append(_build_rule(current_id, fields, field_lines, current_id_line))
        current_id = None
        fields = {}
        field_lines = {}

    try:
        with open(path) as f:
            raw_lines = f.readlines()
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}") from e

    for lineno, raw_line in enumerate(raw_lines, start=1):
        raw = raw_line.rstrip("\n")
        # Reject hard tabs in leading whitespace -- they break our 2/4-space indent model.
        leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in leading:
            raise ConfigError("tab character in indentation; use spaces", lineno)

        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip(" "))

        if folding_key is not None:
            if indent >= 6:
                folded_lines.append(stripped)
                continue
            else:
                fields[folding_key] = " ".join(folded_lines)
                folding_key = None
                folded_lines = []

        if in_extends_block and indent >= 2 and stripped.startswith("-"):
            item = _parse_scalar(stripped[1:].strip())
            if item:
                extends.append(item)
            continue
        elif in_extends_block:
            in_extends_block = False

        if in_skip_block and indent >= 2 and stripped.startswith("-"):
            item = _parse_scalar(stripped[1:].strip())
            if item:
                skip.append(item)
            continue
        elif in_skip_block:
            in_skip_block = False

        if in_execution_block and indent >= 2 and ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value_raw = value.strip()
            if key != "max_workers":
                raise ConfigError(
                    f"unknown execution field '{key}' (allowed: max_workers)",
                    lineno,
                )
            parsed_val = _parse_scalar(value_raw)
            try:
                n = int(parsed_val)
                if n <= 0:
                    raise ValueError
            except (TypeError, ValueError) as e:
                raise ConfigError(
                    f"max_workers must be a positive integer, got {parsed_val!r}",
                    lineno,
                ) from e
            max_workers = n
            continue
        elif in_execution_block:
            in_execution_block = False

        if in_nested_rule_field is not None:
            if indent >= 6 and ":" in stripped:
                nkey, _, nvalue = stripped.partition(":")
                nkey = nkey.strip()
                nvalue_raw = nvalue.strip()
                as_list = _parse_inline_list(nvalue_raw)
                if as_list is not None:
                    nested_rule_field_dict[nkey] = as_list
                    continue
                parsed_nval = _parse_scalar(nvalue_raw)
                if parsed_nval == "true":
                    nested_rule_field_dict[nkey] = True
                    continue
                if parsed_nval == "false":
                    nested_rule_field_dict[nkey] = False
                    continue
                try:
                    nested_rule_field_dict[nkey] = int(parsed_nval)
                except (TypeError, ValueError):
                    nested_rule_field_dict[nkey] = parsed_nval
                continue
            else:
                fields[in_nested_rule_field] = dict(nested_rule_field_dict)
                in_nested_rule_field = None
                nested_rule_field_dict = {}

        if indent == 0:
            if current_id is not None:
                finalize_rule()
            in_rules_block = False

            if stripped == "rules:":
                in_rules_block = True
                continue
            if ":" not in stripped:
                raise ConfigError(f"unexpected top-level line: {stripped!r}", lineno)
            key, _, value = stripped.partition(":")
            key = key.strip()
            value_raw = value.strip()
            if key not in VALID_TOP_LEVEL:
                raise ConfigError(
                    f"unknown top-level key '{key}' "
                    f"(allowed: {', '.join(sorted(VALID_TOP_LEVEL))})",
                    lineno,
                )
            if key == "schema_version":
                v = _parse_scalar(value_raw)
                try:
                    schema_version = int(v)
                except ValueError as e:
                    raise ConfigError(
                        f"schema_version must be an integer, got {v!r}", lineno
                    ) from e
            elif key == "extends":
                as_list = _parse_inline_list(value_raw)
                if as_list is not None:
                    extends.extend(a for a in as_list if a)
                elif value_raw == "":
                    in_extends_block = True
                else:
                    raise ConfigError("extends must be a list like [pack-a, './local.yml']", lineno)
            elif key == "skip":
                as_list = _parse_inline_list(value_raw)
                if as_list is not None:
                    skip.extend(g for g in as_list if g)
                elif value_raw == "":
                    in_skip_block = True
                else:
                    raise ConfigError(
                        'skip must be a list like ["_build/**", "vendor/**"]',
                        lineno,
                    )
            elif key == "execution":
                if value_raw != "":
                    raise ConfigError(
                        "execution must be followed by an indented block",
                        lineno,
                    )
                in_execution_block = True
            continue

        if indent == 2 and stripped.endswith(":"):
            if not in_rules_block:
                raise ConfigError("rule definition outside a `rules:` block", lineno)
            if current_id is not None:
                finalize_rule()
            rid = stripped[:-1].strip()
            if not rid:
                raise ConfigError("empty rule id", lineno)
            if any(ch.isspace() for ch in rid):
                raise ConfigError(f"rule id {rid!r} contains whitespace", lineno)
            current_id = rid
            current_id_line = lineno
            fields = {}
            field_lines = {}
            continue

        if indent == 4 and ":" in stripped:
            if current_id is None:
                raise ConfigError(
                    "field defined outside any rule (indented without a rule id above)",
                    lineno,
                )
            key, _, value = stripped.partition(":")
            key = key.strip()
            value_raw = value.strip()
            if key not in VALID_RULE_FIELDS:
                raise ConfigError(
                    f"unknown rule field '{key}' in rule '{current_id}' "
                    f"(allowed: {', '.join(sorted(VALID_RULE_FIELDS))})",
                    lineno,
                )
            if value_raw == ">":
                folding_key = key
                folded_lines = []
                field_lines[key] = lineno
                continue
            if key in ("context", "when", "require", "capabilities") and value_raw == "":
                in_nested_rule_field = key
                nested_rule_field_dict = {}
                field_lines[key] = lineno
                continue
            as_list = _parse_inline_list(value_raw)
            if as_list is not None:
                fields[key] = as_list
            else:
                fields[key] = _parse_scalar(value_raw)
            field_lines[key] = lineno
            continue

        raise ConfigError(
            f"could not parse line (unexpected indent {indent}): {stripped!r}", lineno
        )

    if folding_key is not None:
        fields[folding_key] = " ".join(folded_lines)
    if in_nested_rule_field is not None:
        fields[in_nested_rule_field] = dict(nested_rule_field_dict)
        in_nested_rule_field = None
        nested_rule_field_dict = {}
    if current_id is not None:
        finalize_rule()

    return ParsedConfig(
        rules=rules,
        extends=extends,
        skip=skip,
        schema_version=schema_version,
        max_workers=max_workers,
    )


def _build_rule(
    rule_id: str,
    fields: dict[str, object],
    field_lines: dict[str, int] | None = None,
    rule_line: int | None = None,
) -> Rule:
    """Build a Rule, validating engine/severity/script. Raises ConfigError on misuse."""
    field_lines = field_lines or {}

    engine = str(fields.get("engine", "script"))
    if engine not in VALID_ENGINES:
        raise ConfigError(
            f"rule '{rule_id}': invalid engine {engine!r} "
            f"(must be 'script', 'semantic', 'ast', or 'session')",
            field_lines.get("engine", rule_line),
        )

    severity = str(fields.get("severity", "error"))
    if severity not in VALID_SEVERITIES:
        raise ConfigError(
            f"rule '{rule_id}': invalid severity {severity!r} (must be 'error' or 'warning')",
            field_lines.get("severity", rule_line),
        )

    script_value = fields.get("script")
    pattern_value = fields.get("pattern")
    language_value = fields.get("language")

    if engine == "script" and script_value is None:
        raise ConfigError(
            f"rule '{rule_id}': engine is 'script' but no 'script' field provided",
            rule_line,
        )
    if engine == "semantic" and script_value is not None:
        raise ConfigError(
            f"rule '{rule_id}': engine is 'semantic' but a 'script' field is set "
            f"(contradiction -- remove one)",
            field_lines.get("script", rule_line),
        )
    if engine == "ast":
        if pattern_value is None:
            raise ConfigError(
                f"rule '{rule_id}': engine is 'ast' but no 'pattern' field provided",
                rule_line,
            )
        if script_value is not None:
            raise ConfigError(
                f"rule '{rule_id}': engine is 'ast' but a 'script' field is set "
                f"(contradiction -- use 'pattern' for ast rules)",
                field_lines.get("script", rule_line),
            )
    if engine != "ast" and pattern_value is not None:
        raise ConfigError(
            f"rule '{rule_id}': 'pattern' is only valid when engine is 'ast'",
            field_lines.get("pattern", rule_line),
        )
    if engine != "ast" and language_value is not None:
        raise ConfigError(
            f"rule '{rule_id}': 'language' is only valid when engine is 'ast'",
            field_lines.get("language", rule_line),
        )

    when_value = fields.get("when")
    require_value = fields.get("require")
    if engine == "session":
        if script_value is not None:
            raise ConfigError(
                f"rule '{rule_id}': engine is 'session' but a 'script' field is set "
                f"(contradiction -- session rules use when/require, not script)",
                field_lines.get("script", rule_line),
            )
        if not isinstance(when_value, dict) or not isinstance(require_value, dict):
            raise ConfigError(
                f"rule '{rule_id}' (session): both 'when' and 'require' must be mappings",
                field_lines.get("when", field_lines.get("require", rule_line)),
            )
    else:
        if when_value is not None:
            raise ConfigError(
                f"rule '{rule_id}': 'when' is only valid when engine is 'session'",
                field_lines.get("when", rule_line),
            )
        if require_value is not None:
            raise ConfigError(
                f"rule '{rule_id}': 'require' is only valid when engine is 'session'",
                field_lines.get("require", rule_line),
            )

    fix_hint_value = fields.get("fix_hint")

    context_value = fields.get("context")
    if context_value is not None and not isinstance(context_value, dict):
        raise ConfigError(
            f"rule '{rule_id}': 'context' must be a mapping (got {type(context_value).__name__})",
            field_lines.get("context", rule_line),
        )

    capabilities_value = fields.get("capabilities")
    if capabilities_value is not None and not isinstance(capabilities_value, dict):
        raise ConfigError(
            f"rule '{rule_id}': 'capabilities' must be a mapping "
            f"(got {type(capabilities_value).__name__})",
            field_lines.get("capabilities", rule_line),
        )

    output_value = fields.get("output")
    if output_value is None:
        output_mode = "parsed"
    else:
        output_mode = str(output_value)
        if output_mode not in VALID_OUTPUT_MODES:
            raise ConfigError(
                f"rule '{rule_id}': invalid output {output_mode!r} "
                f"(must be 'parsed' or 'passthrough')",
                field_lines.get("output", rule_line),
            )
        if engine != "script" and output_mode != "parsed":
            raise ConfigError(
                f"rule '{rule_id}': 'output' is only valid when engine is 'script'",
                field_lines.get("output", rule_line),
            )

    return Rule(
        id=rule_id,
        description=str(fields.get("description", "")),
        engine=engine,
        scope=_normalize_scope(fields.get("scope", "*")),
        severity=severity,
        script=str(script_value) if script_value is not None else None,
        fix_hint=str(fix_hint_value) if fix_hint_value is not None else None,
        pattern=str(pattern_value) if pattern_value is not None else None,
        language=str(language_value) if language_value is not None else None,
        output_mode=output_mode,
        context=dict(context_value) if context_value is not None else None,
        when=dict(when_value) if when_value is not None else None,
        require=dict(require_value) if require_value is not None else None,
        capabilities=(dict(capabilities_value) if isinstance(capabilities_value, dict) else None),
    )
