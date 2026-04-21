"""
Agentic Lint Pipeline

Two-phase evaluation: deterministic script checks, then LLM semantic payload.
Python 3.10+ stdlib only -- no external dependencies.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePath

# ---------------------------------------------------------------------------
# Config schema + parser
# ---------------------------------------------------------------------------

VALID_ENGINES = {"script", "semantic", "ast"}
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
}
VALID_TOP_LEVEL = {"rules", "schema_version", "extends", "skip"}

# User-global ignore file: one glob per line, blank lines and `#` comments
# allowed. Loaded by `effective_skip_patterns` and merged with the built-in
# `SKIP_PATTERNS` plus anything declared in `.bully.yml`.
USER_GLOBAL_IGNORE_FILENAME = ".bully-ignore"

# ast-grep `--lang` values per file extension.
_AST_LANG_BY_EXT: dict[str, str] = {
    ".ts": "ts",
    ".tsx": "tsx",
    ".js": "js",
    ".jsx": "jsx",
    ".mjs": "js",
    ".cjs": "js",
    ".py": "python",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".php": "php",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".scala": "scala",
    ".lua": "lua",
    ".html": "html",
    ".css": "css",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sh": "bash",
    ".bash": "bash",
}

# Files we never want to lint -- lockfiles, minified bundles, generated code.
SKIP_PATTERNS: tuple[str, ...] = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "*.min.js",
    "*.min.css",
    "*.min.*",
    "dist/**",
    "build/**",
    "__pycache__/**",
    "*.generated.*",
    "*.pb.go",
    "*.g.dart",
    "*.freezed.dart",
)


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


@dataclass
class Violation:
    rule: str
    engine: str
    severity: str
    line: int | None
    description: str
    suggestion: str | None = None


# ---------------------------------------------------------------------------
# Scalar/list helpers (unchanged semantics, hardened parser uses them)
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


def _parse_scalar(raw: str) -> str:
    """Normalize a scalar value: strip inline comment, then process YAML quote escapes.

    Double-quoted scalars process standard YAML escapes (`\\\\`, `\\"`, `\\n`, `\\t`,
    `\\r`, `\\/`, `\\0`); unknown escapes are kept verbatim (the backslash is
    preserved) to avoid silently eating author intent. Single-quoted scalars only
    process `''` (doubled single quote) as a literal `'`. Plain unquoted scalars
    pass through unchanged -- backslashes have no special meaning outside quotes.
    """
    raw = _strip_inline_comment(raw).strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return _unescape_double_quoted(raw[1:-1])
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
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
    """Apply YAML double-quoted escape processing to the inside of a scalar.

    Only the subset listed in `_DOUBLE_QUOTED_ESCAPES` is collapsed. Unknown
    sequences (e.g. `\\z`) are kept literally -- we preserve the backslash and
    the following character rather than raising, so rule authors can use
    backslash-heavy regex patterns without the parser throwing at config load.
    A trailing lone backslash is also kept literally.
    """
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
# parse_config with line-numbered errors + extends resolution
# ---------------------------------------------------------------------------


@dataclass
class _ParsedConfig:
    """Internal structure returned by _parse_single_file."""

    rules: list[Rule] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    schema_version: int | None = None


def _parse_single_file(path: str) -> _ParsedConfig:
    """Parse one .bully.yml into _ParsedConfig. Raises ConfigError on malformed input."""
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
    skip: list[str] = []

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

        # Flush folded scalar when dedent happens.
        if folding_key is not None:
            if indent >= 6:
                folded_lines.append(stripped)
                continue
            else:
                fields[folding_key] = " ".join(folded_lines)
                folding_key = None
                folded_lines = []

        # Extends-block continuation: `- item` at indent 2.
        if in_extends_block and indent >= 2 and stripped.startswith("-"):
            item = _parse_scalar(stripped[1:].strip())
            if item:
                extends.append(item)
            continue
        elif in_extends_block:
            in_extends_block = False

        # Skip-block continuation: `- glob` at indent 2.
        if in_skip_block and indent >= 2 and stripped.startswith("-"):
            item = _parse_scalar(stripped[1:].strip())
            if item:
                skip.append(item)
            continue
        elif in_skip_block:
            in_skip_block = False

        # Top-level key (indent 0).
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
            # `rules:` handled above; anything else would have raised already.
            continue

        # Rule id (indent 2).
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

        # Rule field (indent 4).
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
            as_list = _parse_inline_list(value_raw)
            if as_list is not None:
                fields[key] = as_list
            else:
                fields[key] = _parse_scalar(value_raw)
            field_lines[key] = lineno
            continue

        # Anything else is unrecognized indentation.
        raise ConfigError(
            f"could not parse line (unexpected indent {indent}): {stripped!r}", lineno
        )

    # Flush tail state.
    if folding_key is not None:
        fields[folding_key] = " ".join(folded_lines)
    if current_id is not None:
        finalize_rule()

    return _ParsedConfig(
        rules=rules,
        extends=extends,
        skip=skip,
        schema_version=schema_version,
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
            f"rule '{rule_id}': invalid engine {engine!r} (must be 'script', 'semantic', or 'ast')",
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

    fix_hint_value = fields.get("fix_hint")

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
    )


def _normalize_scope(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    if value is None:
        return ("*",)
    return (str(value),)


def _resolve_extends_target(spec: str, config_path: str) -> Path:
    """Resolve an extends reference to an absolute Path."""
    config_dir = Path(config_path).resolve().parent
    p = Path(spec)
    if p.is_absolute():
        return p.resolve()
    return (config_dir / p).resolve()


def _collect_config_files(path: str, visited: list[str] | None = None) -> list[Path]:
    """Return the absolute paths of a config plus every file it extends.

    Resolution order matches `_load_with_extends`: parents first, then self.
    Used by the trust gate to compute a single checksum over the full
    effective config.
    """
    visited = visited or []
    abs_path = Path(path).resolve()
    if str(abs_path) in visited:
        return []
    visited = visited + [str(abs_path)]
    if not abs_path.is_file():
        return []
    try:
        parsed = _parse_single_file(str(abs_path))
    except ConfigError:
        return [abs_path]
    collected: list[Path] = []
    for spec in parsed.extends:
        target = _resolve_extends_target(spec, str(abs_path))
        collected.extend(_collect_config_files(str(target), visited))
    collected.append(abs_path)
    return collected


def parse_config(path: str) -> list[Rule]:
    """Parse .bully.yml into Rule objects, resolving `extends:` transitively.

    Local rules override same-id rules pulled in via extends (warn on stderr).
    Raises ConfigError on cycles, unknown keys/fields, invalid enums, etc.
    """
    resolved = _load_with_extends(path, visited=[])
    return resolved


def _load_with_extends(path: str, visited: list[str]) -> list[Rule]:
    """Recursively load a config + its extends. Returns merged rule list."""
    abs_path = str(Path(path).resolve())
    if abs_path in visited:
        cycle = " -> ".join(visited + [abs_path])
        raise ConfigError(f"extends cycle detected: {cycle}")
    visited = visited + [abs_path]

    parsed = _parse_single_file(path)

    # Pull in extends in order.
    merged: dict[str, Rule] = {}
    order: list[str] = []
    for spec in parsed.extends:
        target = _resolve_extends_target(spec, path)
        if not target.exists():
            raise ConfigError(f"extends target not found: {spec} (resolved to {target})")
        inherited = _load_with_extends(str(target), visited)
        for r in inherited:
            if r.id not in merged:
                order.append(r.id)
            merged[r.id] = r

    # Local rules override.
    for r in parsed.rules:
        if r.id in merged:
            sys.stderr.write(f"bully: rule {r.id} overridden by local config\n")
        else:
            order.append(r.id)
        merged[r.id] = r

    return [merged[rid] for rid in order]


# ---------------------------------------------------------------------------
# File filtering
# ---------------------------------------------------------------------------


def _path_matches_skip(
    file_path: str,
    extra_patterns: tuple[str, ...] | list[str] = (),
) -> bool:
    """Return True if the path matches any built-in or extra skip pattern."""
    p = PurePath(file_path)
    name = p.name
    posix = p.as_posix()
    for pat in (*SKIP_PATTERNS, *extra_patterns):
        # Match basename (covers `*.min.js`, `package-lock.json`, etc.)
        if fnmatch.fnmatch(name, pat):
            return True
        # Match full posix path (covers `dist/**`, etc.)
        if fnmatch.fnmatch(posix, pat):
            return True
        # PurePath.match handles `**` correctly for path-suffix matches.
        try:
            if p.match(pat):
                return True
        except ValueError:
            pass
        # `dist/**` style -- check any segment equals prefix.
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if prefix in p.parts:
                return True
    return False


def _load_user_global_skips() -> list[str]:
    """Load globs from `~/.bully-ignore` (one per line, `#` comments allowed).

    Missing or unreadable files yield an empty list -- this is a per-user
    convenience, never a hard requirement.
    """
    path = Path.home() / USER_GLOBAL_IGNORE_FILENAME
    if not path.is_file():
        return []
    try:
        raw = path.read_text()
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _collect_skip_with_extends(path: str, visited: list[str] | None = None) -> list[str]:
    """Walk a config and its extends chain, collecting `skip:` entries in order.

    Parents are visited first so child configs can append (the merge order in
    `effective_skip_patterns` makes both equivalent for matching, but we keep
    declaration order for predictable doctor output).
    """
    visited = visited or []
    abs_path = str(Path(path).resolve())
    if abs_path in visited:
        return []
    visited = visited + [abs_path]
    if not Path(abs_path).is_file():
        return []
    try:
        parsed = _parse_single_file(abs_path)
    except ConfigError:
        return []
    out: list[str] = []
    for spec in parsed.extends:
        target = _resolve_extends_target(spec, abs_path)
        out.extend(_collect_skip_with_extends(str(target), visited))
    out.extend(parsed.skip)
    return out


def effective_skip_patterns(
    config_path: str,
    *,
    include_user_global: bool = True,
) -> tuple[str, ...]:
    """Return the merged tuple of built-in + user-global + project skip globs.

    Order: built-in defaults, then `~/.bully-ignore` (when enabled), then
    every `skip:` entry pulled from the config and its extends chain.
    Duplicates are preserved -- `_path_matches_skip` short-circuits on the
    first match.
    """
    project: list[str] = []
    if config_path and Path(config_path).is_file():
        project = _collect_skip_with_extends(config_path)
    user_global = _load_user_global_skips() if include_user_global else []
    return (*SKIP_PATTERNS, *user_global, *project)


def _scope_glob_matches(pattern: str, file_path: str) -> bool:
    """Match a scope glob against a file path, with recursive `**` support.

    `PurePath.match` only grew zero-or-more-segment `**` semantics in Python
    3.13; bully supports 3.10+. We split the pattern on `**` and require each
    segment to match contiguously against the path, with `**` absorbing zero
    or more intermediate path segments. Single `*` still only matches within
    one segment (via fnmatch).

    Simple patterns without `**` (the common case) fall back to
    `PurePath.match`, which handles right-anchored suffix matches like
    `*.ts` matching `src/foo.ts`.
    """
    if "**" not in pattern:
        try:
            return PurePath(file_path).match(pattern)
        except ValueError:
            return False

    path_parts = PurePath(file_path).parts
    # Split on `**` but keep empty strings at the boundaries so a leading or
    # trailing `**` is explicit in the segment list.
    raw_segments = pattern.split("**")
    # Each non-`**` segment can contain `/`; split further into path-segment
    # globs. An empty segment (between two consecutive `**`, or at the edges
    # of the pattern) yields [].
    segments: list[list[str]] = []
    for raw in raw_segments:
        trimmed = raw.strip("/")
        segments.append(trimmed.split("/") if trimmed else [])

    # Anchored prefix: the first segment must match starting at path_parts[0]
    # UNLESS the pattern starts with `**/` (i.e. segments[0] is empty), in
    # which case `**` can absorb zero-or-more leading path parts.
    return _match_glob_segments(segments, 0, path_parts, 0)


def _segment_matches(globs: list[str], parts: tuple[str, ...], start: int) -> bool:
    """True iff every glob in `globs` matches `parts[start:start+len(globs)]`."""
    if start + len(globs) > len(parts):
        return False
    for i, g in enumerate(globs):
        if not fnmatch.fnmatchcase(parts[start + i], g):
            return False
    return True


def _match_glob_segments(
    segments: list[list[str]],
    seg_idx: int,
    parts: tuple[str, ...],
    part_idx: int,
) -> bool:
    """Recursively match `**`-delimited glob segments against path parts.

    `segments[0]` is anchored (must match at `part_idx`). Each subsequent
    `segments[i]` is preceded by a `**` and may float -- it can start at any
    position at or after `part_idx`. An empty segment between two `**`
    markers is a no-op. After matching the last segment the remaining path
    parts must be fully consumed (len(parts) == part_idx + len(globs)) unless
    the pattern ended with `**`, in which case they're absorbed.
    """
    if seg_idx >= len(segments):
        # Consumed all segments; remaining path parts must be empty.
        return part_idx == len(parts)

    globs = segments[seg_idx]
    is_last = seg_idx == len(segments) - 1
    # Pattern ended with `**` (trailing empty segment) means the final
    # segment list is empty and `**` can absorb all remaining parts.
    trailing_double_star = is_last and not globs

    if seg_idx == 0:
        # Anchored at part_idx (which is 0 on the initial call). An empty
        # first segment means the pattern starts with `**`, so the next
        # segment floats.
        if not globs:
            return _match_glob_segments(segments, seg_idx + 1, parts, part_idx)
        if not _segment_matches(globs, parts, part_idx):
            return False
        new_idx = part_idx + len(globs)
        if is_last:
            return new_idx == len(parts)
        return _match_glob_segments(segments, seg_idx + 1, parts, new_idx)

    # Floating segment (preceded by `**`). Try every possible start position
    # at or after part_idx. `**` can absorb zero or more path parts.
    if trailing_double_star:
        # Trailing `**` matches anything from part_idx onward, so any path
        # parts remaining (zero or more) are absorbed.
        return True

    if not globs:
        # Empty floating segment (consecutive `**`s). Collapse and continue.
        return _match_glob_segments(segments, seg_idx + 1, parts, part_idx)

    end_limit = len(parts) - len(globs)
    if is_last:
        # Last segment must consume exactly to the end of parts.
        return _segment_matches(globs, parts, end_limit) if end_limit >= part_idx else False
    for try_at in range(part_idx, end_limit + 1):
        if _segment_matches(globs, parts, try_at):
            if _match_glob_segments(segments, seg_idx + 1, parts, try_at + len(globs)):
                return True
    return False


def filter_rules(rules: list[Rule], file_path: str) -> list[Rule]:
    """Return rules whose scope glob(s) match the given file path.

    Scope matching supports recursive `**` (zero-or-more path segments) in
    addition to the standard `*` (single-segment) and `?` wildcards. This is
    implemented in-house because `PurePath.match` only gained recursive `**`
    semantics in Python 3.13 and bully supports 3.10+.
    """
    return [r for r in rules if any(_scope_glob_matches(g, file_path) for g in r.scope)]


# ---------------------------------------------------------------------------
# Diff context builder
# ---------------------------------------------------------------------------

# Write-mode content cap markers.
_WRITE_HEAD_LINES = 100
_WRITE_TAIL_LINES = 50
_WRITE_MAX_LINES = 200

# Synthetic-line warning marker.
SYNTHETIC_MARKER = "# WARNING: synthetic line numbers -- could not anchor diff to file on disk"


def build_diff_context(
    tool_name: str,
    file_path: str,
    old_string: str,
    new_string: str,
    context_lines: int = 5,
) -> str:
    """Produce a diff with real file line numbers for the semantic payload.

    Falls back to a synthetic diff (with a warning marker) when anchoring fails.
    For Write mode, caps very large files to head+tail slices.
    """
    try:
        with open(file_path) as f:
            current = f.read()
    except OSError:
        if tool_name == "Write":
            return _cap_write_content(new_string)
        return (
            f"{SYNTHETIC_MARKER}\n"
            f"--- {file_path} (file not readable)\n+++ edit\n-{old_string}\n+{new_string}\n"
        )

    if tool_name == "Write":
        return _cap_write_content(current)

    # Edit path: synthesize before state
    if new_string and new_string in current:
        before = current.replace(new_string, old_string, 1)
    elif old_string and old_string in current:
        before = current
        current = current.replace(old_string, new_string, 1)
    else:
        # Can't anchor to file; return a best-effort synthetic diff.
        before_lines = (old_string or "").splitlines(keepends=True) or ["\n"]
        after_lines = (new_string or "").splitlines(keepends=True) or ["\n"]
        synth = "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"{file_path}.before",
                tofile=f"{file_path}.after",
                n=context_lines,
            )
        )
        return SYNTHETIC_MARKER + "\n" + synth

    before_lines = before.splitlines(keepends=True)
    after_lines = current.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{file_path}.before",
            tofile=f"{file_path}.after",
            n=context_lines,
        )
    )


def _cap_write_content(content: str) -> str:
    """Return line-numbered content; if too long, slice head + tail with a marker."""
    lines = content.splitlines()
    total = len(lines)
    if total <= _WRITE_MAX_LINES:
        return _line_number(content)

    width = max(3, len(str(total)))
    head = lines[:_WRITE_HEAD_LINES]
    tail = lines[total - _WRITE_TAIL_LINES :]
    out: list[str] = []
    for i, line in enumerate(head, start=1):
        out.append(f"{i:>{width}}: {line}")
    truncated = total - _WRITE_HEAD_LINES - _WRITE_TAIL_LINES
    out.append(f"... {truncated} lines truncated ...")
    tail_start = total - _WRITE_TAIL_LINES + 1
    for i, line in enumerate(tail, start=tail_start):
        out.append(f"{i:>{width}}: {line}")
    return "\n".join(out)


def _was_write_truncated(content: str) -> bool:
    return len(content.splitlines()) > _WRITE_MAX_LINES


def _line_number(content: str) -> str:
    """Prefix each line with `NNNN:` for line-anchored evaluation."""
    lines = content.splitlines()
    width = max(3, len(str(len(lines))))
    return "\n".join(f"{i:>{width}}: {line}" for i, line in enumerate(lines, start=1))


# ---------------------------------------------------------------------------
# Script output parsing
# ---------------------------------------------------------------------------

_FILE_LINE_COL = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$")
_FILE_LINE = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):\s*(?P<msg>.+)$")
_LINE_CONTENT = re.compile(r"^(?P<line>\d+)[:\s-]+(?P<msg>.*)$")


def _violation_from_dict(rule_id: str, severity: str, d: dict) -> Violation | None:
    line = d.get("line") or d.get("lineNumber") or d.get("line_no")
    message = d.get("message") or d.get("msg") or d.get("description") or ""
    if line is None and not message:
        return None
    try:
        line_i = int(line) if line is not None else None
    except (TypeError, ValueError):
        line_i = None
    return Violation(
        rule=rule_id,
        engine="script",
        severity=severity,
        line=line_i,
        description=str(message).strip(),
    )


def parse_script_output(rule_id: str, severity: str, output: str) -> list[Violation]:
    """Parse common tool output formats into Violation records."""
    stripped = output.strip()
    if not stripped:
        return []

    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            v = _violation_from_dict(rule_id, severity, parsed)
            if v is not None:
                return [v]
        elif isinstance(parsed, list):
            vs = [
                _violation_from_dict(rule_id, severity, item)
                for item in parsed
                if isinstance(item, dict)
            ]
            vs = [v for v in vs if v is not None]
            if vs:
                return vs

    violations: list[Violation] = []
    unmatched: list[str] = []
    for line in stripped.splitlines():
        if not line.strip():
            continue
        m = _FILE_LINE_COL.match(line) or _FILE_LINE.match(line)
        if m:
            violations.append(
                Violation(
                    rule=rule_id,
                    engine="script",
                    severity=severity,
                    line=int(m.group("line")),
                    description=m.group("msg").strip(),
                )
            )
            continue
        m = _LINE_CONTENT.match(line)
        if m:
            violations.append(
                Violation(
                    rule=rule_id,
                    engine="script",
                    severity=severity,
                    line=int(m.group("line")),
                    description=m.group("msg").strip(),
                )
            )
            continue
        unmatched.append(line)

    if violations:
        return violations

    return [
        Violation(
            rule=rule_id,
            engine="script",
            severity=severity,
            line=None,
            description=" ".join(unmatched)[:500],
        )
    ]


def execute_script_rule(rule: Rule, file_path: str, diff: str) -> list[Violation]:
    """Run a script-engine rule against a file."""
    cmd = rule.script.replace("{file}", shlex.quote(file_path))
    try:
        # bully-disable: no-shell-true-subprocess script-engine contract; cmd is shlex.quote'd above
        result = subprocess.run(
            cmd,
            shell=True,
            input=diff,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return [
            Violation(
                rule=rule.id,
                engine="script",
                severity=rule.severity,
                line=None,
                description=f"Script timed out after 30s: {cmd}",
            )
        ]

    if result.returncode != 0:
        violations = parse_script_output(rule.id, rule.severity, result.stdout)
        if not violations:
            return [
                Violation(
                    rule=rule.id,
                    engine="script",
                    severity=rule.severity,
                    line=None,
                    description=rule.description,
                )
            ]
        return violations

    return []


# ---------------------------------------------------------------------------
# AST rule execution (ast-grep)
# ---------------------------------------------------------------------------


_AST_GREP_INSTALL_HINT = "install ast-grep: brew install ast-grep  (or: cargo install ast-grep)"


def _infer_ast_language(file_path: str) -> str | None:
    """Infer the ast-grep --lang value from a file path. Returns None if unknown."""
    suffix = PurePath(file_path).suffix.lower()
    return _AST_LANG_BY_EXT.get(suffix)


def ast_grep_available() -> bool:
    """Return True iff `ast-grep` is on PATH."""
    return shutil.which("ast-grep") is not None


def _parse_ast_grep_json(rule_id: str, severity: str, stdout: str) -> list[Violation]:
    """Parse ast-grep's --json output into Violations.

    ast-grep emits a JSON array. Each match has `range.start.line` (0-indexed)
    and `lines` (the matched source text). An empty array means no matches.
    """
    stripped = stdout.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    violations: list[Violation] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        rng = item.get("range") or {}
        start = rng.get("start") if isinstance(rng, dict) else None
        line_i: int | None = None
        if isinstance(start, dict):
            raw_line = start.get("line")
            if isinstance(raw_line, int):
                # ast-grep line numbers are 0-indexed; convert to 1-indexed.
                line_i = raw_line + 1
        matched = item.get("lines") or item.get("text") or ""
        description = str(matched).splitlines()[0].strip() if matched else ""
        violations.append(
            Violation(
                rule=rule_id,
                engine="ast",
                severity=severity,
                line=line_i,
                description=description[:500],
            )
        )
    return violations


def execute_ast_rule(rule: Rule, file_path: str) -> list[Violation]:
    """Run an ast-engine rule against a file via ast-grep.

    Caller is responsible for checking `ast_grep_available()` beforehand and
    handling the missing-tool path. This function assumes the binary exists
    and returns [] on any execution error (conservative: don't block edits
    due to tooling failure).
    """
    lang = rule.language or _infer_ast_language(file_path)
    if lang is None:
        return [
            Violation(
                rule=rule.id,
                engine="ast",
                severity=rule.severity,
                line=None,
                description=(
                    f"ast-grep: could not infer --lang from path {file_path!r}; "
                    "set `language:` on the rule"
                ),
            )
        ]

    cmd = [
        "ast-grep",
        "run",
        "--pattern",
        rule.pattern or "",
        "--lang",
        lang,
        "--json=compact",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return [
            Violation(
                rule=rule.id,
                engine="ast",
                severity=rule.severity,
                line=None,
                description=f"ast-grep timed out after 30s for pattern: {rule.pattern!r}",
            )
        ]
    except FileNotFoundError:
        # ast-grep disappeared between the PATH check and now. Treat as no-op.
        return []

    if result.returncode not in (0, 1):
        # 0 = no matches, 1 = matches (or sometimes error). We only trust stdout.
        stderr_tail = (result.stderr or "").strip().splitlines()[-1:]
        hint = stderr_tail[0] if stderr_tail else ""
        return [
            Violation(
                rule=rule.id,
                engine="ast",
                severity=rule.severity,
                line=None,
                description=f"ast-grep failed (exit {result.returncode}): {hint}"[:500],
            )
        ]

    return _parse_ast_grep_json(rule.id, rule.severity, result.stdout)


# ---------------------------------------------------------------------------
# Semantic payload + pipeline-side can't-match filters
# ---------------------------------------------------------------------------

_COMMENT_LINE_RE = re.compile(r"^\s*(?://|#|--)|^\s*/\*|^\s*\*/|^\s*\*\s")

_ADD_PERSPECTIVE_HINTS = ("avoid", "no ", "no-", "ban", "don't", "dont", "forbid")


def _hunk_added_lines(diff: str) -> list[str]:
    """Return lines added in the diff (lines starting with `+` but not `+++`)."""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            out.append(line[1:])
    return out


def _hunk_removed_lines(diff: str) -> list[str]:
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("---"):
            continue
        if line.startswith("-"):
            out.append(line[1:])
    return out


def _all_whitespace(lines: list[str]) -> bool:
    return all(not line.strip() for line in lines)


def _all_comment(lines: list[str]) -> bool:
    if not lines:
        return False
    return all(_COMMENT_LINE_RE.match(line) or not line.strip() for line in lines)


def _rule_add_perspective(description: str) -> bool:
    d = description.lower()
    return any(h in d for h in _ADD_PERSPECTIVE_HINTS)


def _can_match_diff(rule: Rule, diff: str) -> tuple[bool, str]:
    """Return (should_evaluate, skip_reason_if_not)."""
    if not diff.strip():
        return False, "empty-diff"

    added = _hunk_added_lines(diff)
    removed = _hunk_removed_lines(diff)

    if added and _all_whitespace(added):
        return False, "whitespace-only-additions"

    if added and _all_comment(added) and "comment" not in rule.description.lower():
        return False, "comment-only-additions"

    if not added and removed and _rule_add_perspective(rule.description):
        return False, "pure-deletion-add-perspective-rule"

    if len(added) < 2 and not removed:
        return False, "too-few-added-lines"

    if added and len(added) < 2:
        return False, "too-few-added-lines"

    return True, ""


def build_semantic_payload(
    file_path: str,
    diff: str,
    passed_checks: list[str],
    semantic_rules: list[Rule],
) -> dict:
    """Build the payload the LLM uses for semantic evaluation.

    Structure intentionally separates the subagent-only input
    (`_evaluator_input`) from the full payload (which still carries
    `passed_checks` for the parent). The skill can strip the full payload
    to `_evaluator_input` before dispatching.
    """
    evaluate = [
        {"id": r.id, "description": r.description, "severity": r.severity} for r in semantic_rules
    ]
    payload = {
        "file": file_path,
        "diff": diff,
        "passed_checks": passed_checks,
        "evaluate": evaluate,
    }
    if SYNTHETIC_MARKER in diff:
        payload["line_anchors"] = "synthetic"

    # Evaluator input strips passed_checks (subagent doesn't use it for judgment).
    payload["_evaluator_input"] = {
        "file": file_path,
        "diff": diff,
        "evaluate": evaluate,
    }
    if SYNTHETIC_MARKER in diff:
        payload["_evaluator_input"]["line_anchors"] = "synthetic"
    return payload


# ---------------------------------------------------------------------------
# Baseline + per-line disables
# ---------------------------------------------------------------------------

_DISABLE_RE = re.compile(r"bully-disable\s*:?\s*(?P<ids>[^#\n\r]*?)(?:\s+(?P<reason>[^#\n\r]+))?$")


def _baseline_path(config_path: str) -> Path:
    return Path(config_path).resolve().parent / ".bully" / "baseline.json"


def _load_baseline(config_path: str) -> dict:
    p = _baseline_path(config_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[tuple[str, str, int, str], bool] = {}
    for entry in data.get("baseline", []):
        key = (
            entry.get("rule_id", ""),
            entry.get("file", ""),
            int(entry.get("line", 0) or 0),
            entry.get("checksum", ""),
        )
        out[key] = True
    return out


def _line_checksum(file_path: str, line: int | None) -> str:
    if line is None or line <= 0:
        return ""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            for i, content in enumerate(f, start=1):
                if i == line:
                    return hashlib.sha256(content.encode("utf-8")).hexdigest()
    except OSError:
        return ""
    return ""


def _is_baselined(
    baseline: dict, rule_id: str, config_path: str, file_path: str, line: int | None
) -> bool:
    if not baseline or line is None:
        return False
    try:
        rel = str(Path(file_path).resolve().relative_to(Path(config_path).resolve().parent))
    except ValueError:
        rel = file_path
    checksum = _line_checksum(file_path, line)
    if not checksum:
        return False
    return (rule_id, rel, line, checksum) in baseline


def _parse_disable_directive(text: str) -> tuple[set[str] | None, str | None]:
    """Extract rule ids from an `bully-disable:` comment. Empty set = disable all."""
    m = _DISABLE_RE.search(text)
    if not m:
        return None, None
    ids_raw = (m.group("ids") or "").strip()
    reason = (m.group("reason") or "").strip() or None
    if not ids_raw:
        return set(), reason
    ids = {s.strip().rstrip(",") for s in re.split(r"[,\s]+", ids_raw) if s.strip()}
    return ids, reason


def _line_has_disable(file_path: str, line: int | None, rule_id: str) -> bool:
    """Return True if the violation line or the previous line carries a disable directive."""
    if line is None or line <= 0:
        return False
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content_lines = f.readlines()
    except OSError:
        return False

    targets: list[str] = []
    if line - 1 < len(content_lines):
        targets.append(content_lines[line - 1])
    if line - 2 >= 0 and line - 2 < len(content_lines):
        targets.append(content_lines[line - 2])

    for text in targets:
        ids, _reason = _parse_disable_directive(text)
        if ids is None:
            continue
        if not ids or rule_id in ids:
            return True
    return False


# ---------------------------------------------------------------------------
# Trust boundary: per-machine allowlist for .bully.yml configs
# ---------------------------------------------------------------------------
#
# A `.bully.yml` can execute arbitrary shell commands via `engine: script`
# rules. Cloning a repo with a malicious `.bully.yml` and making any edit
# would run attacker-controlled code in the developer's shell. The trust
# gate prevents this: the first time bully sees a config on a given machine,
# it refuses to execute any rules until the user runs `bully trust`. After
# trust, the gate verifies the checksum on every run -- any change to the
# config (or any extended config) re-requires explicit trust.
#
# Trust state is machine-local (`~/.bully-trust.json`), never committed to
# repos. `BULLY_TRUST_ALL=1` bypasses the gate for CI and first-time setup
# scripts that have already reviewed the config through other means.


_TRUST_ENV_VAR = "BULLY_TRUST_ALL"


def _trust_store_path() -> Path:
    """Per-machine allowlist location."""
    override = os.environ.get("BULLY_TRUST_STORE")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".bully-trust.json"


def _config_checksum(config_path: str) -> str:
    """SHA256 over the concatenated bytes of a config and all its `extends:` targets.

    Returns '' when the top-level config is unreadable.
    """
    files = _collect_config_files(config_path)
    if not files:
        return ""
    h = hashlib.sha256()
    for f in files:
        try:
            h.update(f.read_bytes())
            # Domain separator prevents collisions across different file splits.
            h.update(b"\x00")
        except OSError:
            return ""
    return h.hexdigest()


def _load_trust_store() -> dict:
    """Parse the trust store. Returns {} on any read or parse error."""
    p = _trust_store_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_trust_store(store: dict) -> None:
    """Write the trust store, creating parent dirs as needed."""
    p = _trust_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def _trust_status(config_path: str) -> tuple[str, str]:
    """Return (status, detail). Status is one of: 'trusted', 'untrusted', 'mismatch'.

    'untrusted' means the config has never been trusted on this machine.
    'mismatch' means it was trusted, but the contents have since changed.
    """
    if os.environ.get(_TRUST_ENV_VAR) == "1":
        return "trusted", "env:BULLY_TRUST_ALL"
    abs_path = str(Path(config_path).resolve())
    checksum = _config_checksum(abs_path)
    if not checksum:
        return "untrusted", "cannot read config"
    store = _load_trust_store()
    entry = store.get("allowed", {}).get(abs_path)
    if not isinstance(entry, dict):
        return "untrusted", "never trusted"
    recorded = entry.get("checksum", "")
    if recorded != checksum:
        return "mismatch", f"checksum changed (was {recorded[:12]}..., now {checksum[:12]}...)"
    return "trusted", recorded[:12] + "..."


def _untrusted_stderr(config_path: str, status: str, detail: str) -> str:
    """Rendered stderr message for untrusted/mismatched configs."""
    abs_path = Path(config_path).resolve()
    if status == "mismatch":
        headline = f"bully: {abs_path} changed since last trust ({detail})."
        action = "Re-review the config, then run: bully trust --refresh"
    else:
        headline = f"bully: {abs_path} is not trusted on this machine."
        action = "Review the config, then run: bully trust"
    return (
        f"{headline}\n"
        f"Scripts in .bully.yml execute on your machine. "
        f"Until trusted, rules will not run. Edits are not blocked.\n"
        f"{action}\n"
        f"(To allow all configs unconditionally -- not recommended -- "
        f"set {_TRUST_ENV_VAR}=1.)\n"
    )


def _cmd_trust(config_path: str | None, refresh: bool) -> int:
    """Record the current config's checksum in the trust store."""
    path = config_path or ".bully.yml"
    abs_path = Path(path).resolve()
    if not abs_path.is_file():
        print(f"config not found: {abs_path}", file=sys.stderr)
        return 1
    checksum = _config_checksum(str(abs_path))
    if not checksum:
        print(f"cannot checksum config at {abs_path}", file=sys.stderr)
        return 1

    store = _load_trust_store()
    allowed = store.setdefault("allowed", {})
    existing = allowed.get(str(abs_path))
    if isinstance(existing, dict) and existing.get("checksum") == checksum and not refresh:
        print(f"already trusted: {abs_path}  sha256={checksum[:12]}...")
        return 0
    allowed[str(abs_path)] = {
        "checksum": checksum,
        "allowed_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    _save_trust_store(store)
    verb = "updated" if existing else "trusted"
    print(f"{verb}: {abs_path}  sha256={checksum[:12]}...")
    return 0


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def _telemetry_path(config_path: str) -> Path | None:
    """Return the telemetry log path if telemetry is enabled for this project."""
    project_dir = Path(config_path).resolve().parent
    tel_dir = project_dir / ".bully"
    if not tel_dir.is_dir():
        return None
    return tel_dir / "log.jsonl"


def _append_telemetry(
    log_path: Path,
    file_path: str,
    status: str,
    rule_records: list[dict],
    latency_ms: int,
) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "file": file_path,
        "status": status,
        "latency_ms": latency_ms,
        "rules": rule_records,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _append_record(log_path: Path, record: dict) -> None:
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


class _NoopPhaseTimer:
    """Default phase timer: every call is a no-op context manager."""

    def __call__(self, name: str):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


_NOOP_PHASE_TIMER = _NoopPhaseTimer()


def run_pipeline(
    config_path: str,
    file_path: str,
    diff: str,
    rule_filter: set[str] | None = None,
    *,
    include_skipped: bool = False,
    phase_timer=_NOOP_PHASE_TIMER,
) -> dict:
    """Full two-phase pipeline.

    Phase 1: script rules. If any error-severity violations, block.
    Phase 2: build semantic payload for remaining semantic rules.

    When `include_skipped=True`, the result dict gains two extra fields:
    `semantic_skipped` (a list of `{"rule", "reason"}` for every semantic rule
    the can't-match heuristics dropped) and `rules_evaluated` (a list of
    `{"rule", "engine", "verdict", "reason"?}` for every rule in scope).
    Both are intentionally gated -- hook-mode output stays unchanged.
    """
    start = time.perf_counter()
    rule_records: list[dict] = []
    log_path = _telemetry_path(config_path)

    # Short-circuit auto-generated files (built-in + user-global + project skip).
    with phase_timer("skip_check"):
        extra_skip = effective_skip_patterns(config_path)[len(SKIP_PATTERNS) :]
        if _path_matches_skip(file_path, extra_patterns=extra_skip):
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            result = {"status": "skipped", "file": file_path, "reason": "auto-generated"}
            if log_path is not None:
                _append_telemetry(log_path, file_path, "skipped", rule_records, elapsed_ms)
            return result

    # Trust gate: refuse to execute any rules from an un-reviewed config.
    with phase_timer("trust_gate"):
        trust_status, trust_detail = _trust_status(config_path)
        if trust_status != "trusted":
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            result = {
                "status": "untrusted",
                "file": file_path,
                "config": str(Path(config_path).resolve()),
                "trust_status": trust_status,
                "trust_detail": trust_detail,
            }
            if log_path is not None:
                _append_telemetry(
                    log_path, file_path, f"untrusted:{trust_status}", rule_records, elapsed_ms
                )
            return result

    with phase_timer("parse_config"):
        rules = parse_config(config_path)
    with phase_timer("filter_rules"):
        matching = filter_rules(rules, file_path)
        if rule_filter:
            matching = [r for r in matching if r.id in rule_filter]

    def flush(status: str, result: dict) -> dict:
        if log_path is not None:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            _append_telemetry(log_path, file_path, status, rule_records, elapsed_ms)
        return result

    if not matching:
        return flush("pass", {"status": "pass", "file": file_path})

    script_rules = [r for r in matching if r.engine == "script"]
    ast_rules = [r for r in matching if r.engine == "ast"]
    semantic_rules = [r for r in matching if r.engine == "semantic"]

    all_violations: list[Violation] = []
    passed_checks: list[str] = []
    baseline = _load_baseline(config_path)

    def _run_deterministic(
        rule: Rule,
        executor,
        engine_label: str,
    ) -> None:
        rule_start = time.perf_counter()
        violations = executor()
        rule_ms = int((time.perf_counter() - rule_start) * 1000)

        if rule.fix_hint:
            violations = [replace(v, suggestion=v.suggestion or rule.fix_hint) for v in violations]

        filtered: list[Violation] = []
        for v in violations:
            if _line_has_disable(file_path, v.line, rule.id):
                continue
            if _is_baselined(baseline, rule.id, config_path, file_path, v.line):
                continue
            filtered.append(v)
        violations = filtered

        if violations:
            all_violations.extend(violations)
            rule_records.append(
                {
                    "id": rule.id,
                    "engine": engine_label,
                    "verdict": "violation",
                    "severity": rule.severity,
                    "line": violations[0].line,
                    "latency_ms": rule_ms,
                }
            )
        else:
            passed_checks.append(rule.id)
            rule_records.append(
                {
                    "id": rule.id,
                    "engine": engine_label,
                    "verdict": "pass",
                    "severity": rule.severity,
                    "latency_ms": rule_ms,
                }
            )

    with phase_timer("script_exec"):
        for rule in script_rules:
            _run_deterministic(
                rule,
                lambda r=rule: execute_script_rule(r, file_path, diff),
                "script",
            )

    with phase_timer("ast_exec"):
        if ast_rules:
            if ast_grep_available():
                for rule in ast_rules:
                    _run_deterministic(
                        rule,
                        lambda r=rule: execute_ast_rule(r, file_path),
                        "ast",
                    )
            else:
                sys.stderr.write(
                    "bully: engine:ast rules matched but ast-grep not on PATH; skipping. "
                    f"{_AST_GREP_INSTALL_HINT}\n"
                )
                for rule in ast_rules:
                    rule_records.append(
                        {
                            "id": rule.id,
                            "engine": "ast",
                            "verdict": "skipped",
                            "severity": rule.severity,
                            "reason": "ast-grep-not-installed",
                        }
                    )

    # Can't-match filters for semantic rules.
    with phase_timer("semantic_build"):
        dispatched_semantic: list[Rule] = []
        semantic_skipped: list[dict] = []
        for rule in semantic_rules:
            ok, reason = _can_match_diff(rule, diff)
            if ok:
                dispatched_semantic.append(rule)
                rule_records.append(
                    {
                        "id": rule.id,
                        "engine": "semantic",
                        "verdict": "evaluate_requested",
                        "severity": rule.severity,
                    }
                )
            else:
                semantic_skipped.append({"rule": rule.id, "reason": reason})
                if log_path is not None:
                    _append_record(
                        log_path,
                        {
                            "ts": datetime.now(timezone.utc)
                            .isoformat(timespec="seconds")
                            .replace("+00:00", "Z"),
                            "type": "semantic_skipped",
                            "file": file_path,
                            "rule": rule.id,
                            "reason": reason,
                        },
                    )

    blocking = [v for v in all_violations if v.severity == "error"]

    def _decorate(result: dict) -> dict:
        if not include_skipped:
            return result
        result["semantic_skipped"] = list(semantic_skipped)
        result["rules_evaluated"] = _explain_rules_evaluated(
            rule_records, semantic_skipped, dispatched_semantic
        )
        return result

    if blocking:
        return _decorate(
            flush(
                "blocked",
                {
                    "status": "blocked",
                    "file": file_path,
                    "violations": [asdict(v) for v in all_violations],
                    "passed": passed_checks,
                },
            )
        )

    if dispatched_semantic:
        payload = build_semantic_payload(file_path, diff, passed_checks, dispatched_semantic)
        result = {"status": "evaluate", **payload}
        if _was_write_truncated_for_path(file_path):
            result["write_content"] = "truncated"
        if all_violations:
            result["warnings"] = [asdict(v) for v in all_violations]
        return _decorate(flush("evaluate", result))

    result = {"status": "pass", "file": file_path, "passed": passed_checks}
    if all_violations:
        result["warnings"] = [asdict(v) for v in all_violations]
    return _decorate(flush("pass", result))


def _explain_rules_evaluated(
    rule_records: list[dict],
    semantic_skipped: list[dict],
    dispatched_semantic: list[Rule],
) -> list[dict]:
    """Project the internal `rule_records` into a per-rule verdict line.

    Verdicts: `fire` (deterministic violation), `pass` (deterministic clean
    or semantic dispatched-no-violation), `skipped` (can't-match heuristic
    or ast-grep missing), `dispatched` (semantic rule sent to the evaluator).
    """
    dispatched_ids = {r.id for r in dispatched_semantic}
    out: list[dict] = []
    for rec in rule_records:
        rule_id = rec.get("id", "")
        engine = rec.get("engine", "")
        record_verdict = rec.get("verdict", "")
        if record_verdict == "violation":
            out.append({"rule": rule_id, "engine": engine, "verdict": "fire"})
        elif record_verdict == "pass":
            out.append({"rule": rule_id, "engine": engine, "verdict": "pass"})
        elif record_verdict == "evaluate_requested":
            out.append(
                {
                    "rule": rule_id,
                    "engine": engine,
                    "verdict": "dispatched" if rule_id in dispatched_ids else "pass",
                }
            )
        elif record_verdict == "skipped":
            out.append(
                {
                    "rule": rule_id,
                    "engine": engine,
                    "verdict": "skipped",
                    "reason": rec.get("reason", ""),
                }
            )
    for skip in semantic_skipped:
        out.append(
            {
                "rule": skip["rule"],
                "engine": "semantic",
                "verdict": "skipped",
                "reason": skip["reason"],
            }
        )
    return out


def _print_explain(result: dict, file_path: str) -> None:
    """Render the --explain output: one line per rule in scope.

    Falls back to a clear one-liner when the result has a non-evaluating
    status (skipped, untrusted, no rules in scope) so authors aren't left
    staring at silence.
    """
    status = result.get("status", "")
    print(f"file: {file_path}")
    print(f"status: {status}")
    if status == "skipped":
        print(f"  pipeline skipped (reason: {result.get('reason', 'unknown')})")
        return
    if status == "untrusted":
        print(f"  config not trusted on this machine ({result.get('trust_detail', '')})")
        return
    rules = result.get("rules_evaluated", [])
    if not rules:
        print("  no rules matched the file's scope")
        return
    for r in rules:
        verdict = r.get("verdict", "")
        rule_id = r.get("rule", "")
        engine = r.get("engine", "")
        if verdict == "skipped":
            print(f"  [{engine}] {rule_id}: skipped ({r.get('reason', '')})")
        else:
            print(f"  [{engine}] {rule_id}: {verdict}")


def _was_write_truncated_for_path(file_path: str) -> bool:
    """Cheap stat-only check that doesn't re-read huge files into memory unnecessarily."""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            count = sum(1 for _ in f)
        return count > _WRITE_MAX_LINES
    except OSError:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_blocked_stderr(result: dict) -> str:
    """Render a blocked pipeline result as agent-readable text for stderr."""
    lines = ["AGENTIC LINT -- blocked. Fix these before proceeding:", ""]
    for v in result.get("violations", []):
        line_repr = v.get("line") if v.get("line") is not None else "?"
        lines.append(f"- [{v['rule']}] line {line_repr}: {v['description']}")
        if v.get("suggestion"):
            lines.append(f"  suggestion: {v['suggestion']}")
    passed = result.get("passed", [])
    if passed:
        lines.append("")
        lines.append(f"Passed checks: {', '.join(passed)}")
    return "\n".join(lines) + "\n"


def _read_stdin_payload() -> dict:
    """Read stdin; if JSON, return parsed dict, else wrap as raw diff."""
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return {"diff": raw}


def _build_semantic_prompt(payload: dict) -> str:
    """Render the semantic evaluation payload as a human-readable prompt."""
    lines = [
        f"Evaluate this diff against the rules below. File: {payload.get('file', '?')}",
        "",
    ]
    passed = payload.get("passed_checks", [])
    if passed:
        lines.append(f"Already passed (do not re-evaluate): {', '.join(passed)}")
        lines.append("")
    lines.append("Rules to evaluate:")
    for r in payload.get("evaluate", []):
        lines.append(f"- [{r['id']}] ({r['severity']}): {r['description']}")
    lines.append("")
    lines.append("Diff:")
    lines.append(payload.get("diff", ""))
    lines.append("")
    lines.append(
        "For each violation: rule id, line number, description, fix suggestion. "
        "If no violations, say 'no violations' explicitly."
    )
    return "\n".join(lines)


# Subcommand verbs accepted as the first argv element. Each maps to either a
# flag or a small argv rewrite. Keeps the legacy `--validate`/`--doctor` flags
# and the legacy positional `<config> <file>` form (used by hook.sh) working.
_SUBCOMMAND_FLAGS = {
    "validate": "--validate",
    "doctor": "--doctor",
    "show-resolved-config": "--show-resolved-config",
    "baseline-init": "--baseline-init",
    "trust": "--trust",
}


def _normalize_argv(argv: list[str]) -> list[str]:
    """Translate `bully <verb> ...` shorthand into the underlying flag form.

    - `validate` / `doctor` / `show-resolved-config` / `baseline-init` / `trust`
      become their `--verb` flag equivalents.
    - `lint <path>` becomes `--file <path>` (the rest of argv is preserved).
    - Anything else passes through unchanged so legacy positional and flag
      invocations keep working.
    """
    if not argv:
        return argv
    head = argv[0]
    if head in _SUBCOMMAND_FLAGS:
        return [_SUBCOMMAND_FLAGS[head], *argv[1:]]
    if head == "lint":
        rest = argv[1:]
        if rest and not rest[0].startswith("-"):
            return ["--file", rest[0], *rest[1:]]
        return rest
    return argv


def _parse_args(argv: list[str]) -> argparse.Namespace:
    argv = _normalize_argv(argv)
    parser = argparse.ArgumentParser(
        prog="bully",
        description="Agentic Lint pipeline. Runs script and semantic rules for a file.",
    )
    parser.add_argument("positional", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("--config", help="Path to .bully.yml")
    parser.add_argument("--file", dest="file_path", help="Target file to evaluate")
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        help="Evaluate only this rule id. Repeatable.",
    )
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="Print the LLM prompt text for the semantic payload instead of JSON.",
    )
    parser.add_argument("--diff", help="Inline diff string (bypasses stdin).")
    parser.add_argument(
        "--hook-mode",
        action="store_true",
        help="Read tool-hook JSON on stdin and emit Claude Code hook output.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the config file: parse, check enums, exit nonzero on error.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run diagnostic checks and exit.",
    )
    parser.add_argument(
        "--show-resolved-config",
        action="store_true",
        help="Print merged rules (after resolving extends) as compact text.",
    )
    parser.add_argument(
        "--baseline-init",
        action="store_true",
        help="Run the pipeline over a glob and write current violations to baseline.json.",
    )
    parser.add_argument(
        "--glob",
        default=None,
        help="Glob pattern for --baseline-init (relative to config dir).",
    )
    parser.add_argument(
        "--log-verdict",
        action="store_true",
        help="Append a semantic_verdict telemetry record.",
    )
    parser.add_argument("--verdict", choices=("pass", "violation"), default=None)
    parser.add_argument(
        "--trust",
        action="store_true",
        help="Allow the given --config to execute rules on this machine. "
        "Records a SHA256 checksum; edits to the config re-require --trust.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="With --trust: re-approve a changed config. Without --trust: no-op.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print per-rule verdict (fire/pass/skipped <reason>/dispatched) for "
        "every rule in scope, instead of the JSON pipeline result.",
    )
    parser.add_argument(
        "--execute-dry-run",
        dest="execute_dry_run",
        action="store_true",
        help="With --validate: run each script rule against empty input to catch "
        "shell/regex-level errors (unbalanced parens, missing commands) at config time.",
    )
    args = parser.parse_args(argv)
    # Back-compat: accept positional args (used by hook)
    if args.positional and not args.config:
        args.config = args.positional[0]
    if len(args.positional) >= 2 and not args.file_path:
        args.file_path = args.positional[1]
    return args


# ---- subcommand handlers ----


def _cmd_validate(config_path: str | None, *, execute_dry_run: bool = False) -> int:
    path = config_path or ".bully.yml"
    if not os.path.exists(path):
        print(f"[FAIL] config not found: {path}", file=sys.stderr)
        return 1
    try:
        rules = parse_config(path)
    except ConfigError as e:
        print(f"[FAIL] {path}: {e}", file=sys.stderr)
        return 1
    print(f"[OK] parsed {len(rules)} rule(s) from {path}")
    for r in rules:
        print(f"  - {r.id}  engine={r.engine}  severity={r.severity}  scope={list(r.scope)}")
    ast_rule_ids = [r.id for r in rules if r.engine == "ast"]
    if ast_rule_ids and not ast_grep_available():
        print(
            f"[WARN] {len(ast_rule_ids)} engine:ast rule(s) will be skipped at runtime: "
            f"ast-grep not on PATH. {_AST_GREP_INSTALL_HINT}",
            file=sys.stderr,
        )
    if execute_dry_run:
        return _run_execute_dry_run(rules)
    return 0


def _run_execute_dry_run(rules: list[Rule]) -> int:
    """Execute every script rule against `/dev/null`, report broken scripts.

    Catches shell/regex-level errors at config time: unbalanced parens in a
    `grep -E` pattern, typos in command names, non-executable scripts, etc.
    A rule is flagged as broken when either:

    - The exit code is not in {0, 1} (2 = grep syntax error, 126 = not
      executable, 127 = command-not-found, etc.), OR
    - stderr carries a known tool-error signature even when exit is 0/1.
      This matters because shells often mask inner errors: `grep ... &&
      exit 1 || exit 0` swallows grep's exit-2 and reports 0, leaving the
      regex diagnostic only in stderr.

    Returns 0 if all script rules are healthy, 1 if any were flagged.
    """
    # Prefixes that indicate a tool surfaced an error, not just incidental
    # stderr chatter. Keep narrow to avoid false positives from tools that
    # write progress to stderr.
    error_signatures = (
        "grep:",
        "sed:",
        "awk:",
        "bash:",
        "sh:",
        "command not found",
        "syntax error",
        "not recognized as an internal",
    )

    script_rules = [r for r in rules if r.engine == "script" and r.script]
    if not script_rules:
        print("[OK] no script rules to dry-run")
        return 0

    failures = 0
    for rule in script_rules:
        cmd = rule.script.replace("{file}", "/dev/null")
        try:
            result = subprocess.run(  # bully-disable: no-shell-true-subprocess dry-run probe of user-configured script; mirrors real execute_script_rule path
                cmd,
                shell=True,
                timeout=5,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            print(f"[WARN] {rule.id}: dry-run exit=timeout stderr: script timed out")
            failures += 1
            continue

        rc = result.returncode
        stderr = result.stderr.strip()
        stderr_first = stderr.splitlines()[0] if stderr else ""
        # A stderr line that matches a known error signature means the tool
        # reported an error even if the outer shell construct normalized
        # the exit code back to 0/1.
        stderr_looks_broken = any(sig in stderr.lower() for sig in error_signatures)

        if rc in (0, 1) and not stderr_looks_broken:
            print(f"[OK] {rule.id}: dry-run clean (exit {rc})")
            continue
        failures += 1
        print(f"[WARN] {rule.id}: dry-run exit={rc} stderr: {stderr_first}")

    return 0 if failures == 0 else 1


def _cmd_show_resolved(config_path: str | None) -> int:
    path = config_path or ".bully.yml"
    try:
        rules = parse_config(path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for r in rules:
        print(
            f"{r.id}\tengine={r.engine}\tseverity={r.severity}\t"
            f"scope={','.join(r.scope)}\tfix_hint={r.fix_hint or ''}"
        )
    return 0


_MIN_PYTHON = (3, 10)


def _check_python_version(version_info: tuple[int, int] = sys.version_info[:2]) -> tuple[bool, str]:
    """Return (ok, message) for the Python version check.

    Split out of `_cmd_doctor` so tests can feed synthetic version tuples
    without spawning a different interpreter.
    """
    major, minor = version_info[:2]
    if (major, minor) >= _MIN_PYTHON:
        return True, f"[OK] Python {major}.{minor}"
    need = f"{_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}"
    return False, f"[FAIL] Python {major}.{minor} < {need} -- upgrade required"


def _plugin_cache_candidates(resource_kind: str, name: str) -> list[Path]:
    """Return plausible `~/.claude/plugins/cache/*/bully/*/{skills,agents}/<name>/...` paths.

    resource_kind is "skills" or "agents". For skills, the file is `<name>/SKILL.md`;
    for agents, the file is `<name>.md` directly under `agents/`.
    """
    root = Path.home() / ".claude" / "plugins" / "cache"
    if not root.is_dir():
        return []
    pattern = f"*/bully/*/{resource_kind}/"
    out: list[Path] = []
    for base in root.glob(pattern):
        if resource_kind == "skills":
            candidate = base / name / "SKILL.md"
        else:
            candidate = base / f"{name}.md"
        if candidate.is_file():
            out.append(candidate)
    return out


def _cmd_doctor() -> int:
    ok = True

    # Python version
    py_ok, py_msg = _check_python_version()
    print(py_msg)
    if not py_ok:
        ok = False

    # Config present
    cfg = Path.cwd() / ".bully.yml"
    if cfg.is_file():
        print(f"[OK] config present at {cfg}")
    else:
        print(f"[FAIL] no .bully.yml at {Path.cwd()}")
        ok = False

    # Config parses
    parsed_rules: list[Rule] = []
    if cfg.is_file():
        try:
            parsed_rules = parse_config(str(cfg))
            print(f"[OK] config parses ({len(parsed_rules)} rules)")
        except ConfigError as e:
            print(f"[FAIL] config parse error: {e}")
            ok = False

    # Trust status for the local config (machine-local, not committed).
    if cfg.is_file():
        status, detail = _trust_status(str(cfg))
        if status == "trusted":
            print(f"[OK] config trusted on this machine ({detail})")
        elif status == "mismatch":
            print(
                f"[WARN] config trusted but checksum changed: {detail}. Run: bully trust --refresh"
            )
        else:
            print(
                f"[WARN] config not trusted on this machine ({detail}). "
                "Rules will not run until you run: bully trust"
            )

    # ast-grep availability (only matters if engine:ast rules exist)
    ast_rule_count = sum(1 for r in parsed_rules if r.engine == "ast")
    if ast_rule_count > 0:
        if ast_grep_available():
            print(f"[OK] ast-grep on PATH ({ast_rule_count} engine:ast rule(s))")
        else:
            print(
                f"[FAIL] {ast_rule_count} engine:ast rule(s) need ast-grep. "
                f"{_AST_GREP_INSTALL_HINT}"
            )
            ok = False

    # PostToolUse hook wired in .claude/settings.json
    hook_wired = False
    for settings in (
        Path.cwd() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
    ):
        if not settings.is_file():
            continue
        try:
            data = json.loads(settings.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        hooks = data.get("hooks", {})
        entries = hooks.get("PostToolUse", [])
        if isinstance(entries, list):
            for entry in entries:
                for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                    if "hook.sh" in str(h.get("command", "")):
                        hook_wired = True
                        break
                if hook_wired:
                    break
        if hook_wired:
            print(f"[OK] PostToolUse hook wired in {settings}")
            break
    if not hook_wired:
        print("[FAIL] no PostToolUse hook invoking hook.sh found in .claude/settings.json")
        ok = False

    # Evaluator subagent definition -- legacy path OR plugin cache path
    claude_home = Path(os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude")))
    agent_file = claude_home / "agents" / "bully-evaluator.md"
    plugin_agents = _plugin_cache_candidates("agents", "bully-evaluator")
    if agent_file.is_file():
        print(f"[OK] evaluator agent at {agent_file}")
    elif plugin_agents:
        print(f"[OK] evaluator agent at {plugin_agents[0]} (plugin install)")
    else:
        print(
            f"[FAIL] evaluator agent missing -- expected at {agent_file} "
            f"or under ~/.claude/plugins/cache/*/bully/*/agents/bully-evaluator.md"
        )
        ok = False

    # Skills -- legacy path OR plugin cache path
    for suffix in (
        "bully",
        "bully-init",
        "bully-author",
        "bully-review",
    ):
        skill_md = Path.home() / ".claude" / "skills" / suffix / "SKILL.md"
        plugin_skill = _plugin_cache_candidates("skills", suffix)
        if skill_md.is_file():
            print(f"[OK] skill {suffix} present")
        elif plugin_skill:
            print(f"[OK] skill {suffix} present at {plugin_skill[0]} (plugin install)")
        else:
            print(
                f"[FAIL] skill {suffix} missing -- expected at {skill_md} "
                f"or under ~/.claude/plugins/cache/*/bully/*/skills/{suffix}/SKILL.md"
            )
            ok = False

    return 0 if ok else 1


def _cmd_log_verdict(
    config_path: str | None, rule_id: str, verdict: str, file_path: str | None
) -> int:
    path = config_path or ".bully.yml"
    log_path = _telemetry_path(path)
    if log_path is None:
        print(
            "telemetry disabled (no .bully/ directory next to config)",
            file=sys.stderr,
        )
        return 0
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "type": "semantic_verdict",
        "rule": rule_id,
        "verdict": verdict,
    }
    if file_path:
        record["file"] = file_path
    _append_record(log_path, record)
    return 0


def _cmd_baseline_init(config_path: str | None, glob: str | None) -> int:
    path = config_path or ".bully.yml"
    cfg_abs = Path(path).resolve()
    if not cfg_abs.exists():
        print(f"config not found: {path}", file=sys.stderr)
        return 1
    root = cfg_abs.parent
    if not glob:
        glob = "**/*"
    extra_skip = effective_skip_patterns(str(cfg_abs))[len(SKIP_PATTERNS) :]
    entries: list[dict] = []
    for candidate in root.glob(glob):
        if not candidate.is_file():
            continue
        if _path_matches_skip(str(candidate), extra_patterns=extra_skip):
            continue
        try:
            result = run_pipeline(str(cfg_abs), str(candidate), "")
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 1
        if result.get("status") != "blocked":
            continue
        for v in result.get("violations", []):
            line = v.get("line")
            checksum = _line_checksum(str(candidate), line)
            try:
                rel = str(candidate.resolve().relative_to(root))
            except ValueError:
                rel = str(candidate)
            entries.append(
                {
                    "rule_id": v["rule"],
                    "file": rel,
                    "line": line or 0,
                    "checksum": checksum,
                }
            )
    out_dir = root / ".bully"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "baseline.json"
    out.write_text(json.dumps({"baseline": entries}, indent=2) + "\n")
    print(f"wrote {len(entries)} baseline entries to {out}")
    return 0


# ---- hook-mode + main ----


def _find_config_upward(start: Path) -> Path | None:
    cur = start.resolve()
    if cur.is_file():
        cur = cur.parent
    for p in (cur, *cur.parents):
        candidate = p / ".bully.yml"
        if candidate.is_file():
            return candidate
    return None


def _hook_mode() -> int:
    """Read stdin JSON from Claude Code, run the pipeline, emit hook output."""
    payload = _read_stdin_payload()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    file_path = tool_input.get("file_path") or payload.get("file_path") or ""
    old_string = tool_input.get("old_string", "") or payload.get("old_string", "") or ""
    if tool_name == "Write":
        new_string = (
            tool_input.get("content")
            or tool_input.get("new_string")
            or payload.get("content")
            or payload.get("new_string")
            or ""
        )
    else:
        new_string = tool_input.get("new_string", "") or payload.get("new_string", "") or ""

    if not file_path or not Path(file_path).is_file():
        return 0

    config = _find_config_upward(Path(file_path))
    if config is None:
        return 0

    diff = build_diff_context(
        tool_name=tool_name,
        file_path=file_path,
        old_string=old_string,
        new_string=new_string,
    )

    try:
        result = run_pipeline(str(config), file_path, diff)
    except ConfigError as e:
        sys.stderr.write(f"AGENTIC LINT -- config error: {e}\n")
        return 0

    status = result.get("status", "pass")
    if status == "untrusted":
        sys.stderr.write(
            _untrusted_stderr(
                result.get("config", str(config)),
                result.get("trust_status", "untrusted"),
                result.get("trust_detail", ""),
            )
        )
        return 0
    if status == "blocked":
        sys.stderr.write(_format_blocked_stderr(result))
        return 2
    if status == "evaluate":
        ctx = "AGENTIC LINT SEMANTIC EVALUATION REQUIRED:\n\n" + json.dumps(
            result, separators=(",", ":")
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": ctx,
                    }
                }
            )
        )
    return 0


def main() -> None:
    # Short-circuit: `bully bench ...` dispatches to the bench CLI directly,
    # bypassing the main parser (which uses a flat flag model).
    if len(sys.argv) >= 2 and sys.argv[1] == "bench":
        from pipeline.bench import main as bench_main

        sys.exit(bench_main(sys.argv[2:]))

    args = _parse_args(sys.argv[1:])

    # Subcommands.
    if args.trust:
        sys.exit(_cmd_trust(args.config, refresh=args.refresh))
    if args.validate:
        sys.exit(_cmd_validate(args.config, execute_dry_run=args.execute_dry_run))
    if args.doctor:
        sys.exit(_cmd_doctor())
    if args.show_resolved_config:
        sys.exit(_cmd_show_resolved(args.config))
    if args.baseline_init:
        sys.exit(_cmd_baseline_init(args.config, args.glob))
    if args.log_verdict:
        if not args.rule or not args.verdict:
            print(
                "usage: --log-verdict --rule RULE_ID --verdict pass|violation [--file PATH]",
                file=sys.stderr,
            )
            sys.exit(1)
        rule_id = args.rule[0] if args.rule else ""
        sys.exit(_cmd_log_verdict(args.config, rule_id, args.verdict, args.file_path))
    if args.hook_mode:
        sys.exit(_hook_mode())

    # Default config to ./.bully.yml when a target file is given but no
    # config is specified -- lets `bully lint src/foo.py` work standalone.
    if args.file_path and not args.config and os.path.exists(".bully.yml"):
        args.config = ".bully.yml"

    if not args.config or not args.file_path:
        print(
            json.dumps(
                {
                    "error": "Usage: bully lint <file> [--config <path>] "
                    "(or pipeline.py <config> <file>)"
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = args.config
    file_path = args.file_path

    if not os.path.exists(config_path):
        print(json.dumps({"status": "pass", "file": file_path, "reason": "no config found"}))
        sys.exit(0)

    if args.diff is not None:
        diff = args.diff
    else:
        payload = _read_stdin_payload()
        if "diff" in payload:
            diff = payload["diff"]
        elif "tool_name" in payload:
            tool_input = (
                payload.get("tool_input", {}) if isinstance(payload.get("tool_input"), dict) else {}
            )
            diff = build_diff_context(
                tool_name=payload.get("tool_name", ""),
                file_path=tool_input.get("file_path") or payload.get("file_path", file_path),
                old_string=tool_input.get("old_string") or payload.get("old_string", ""),
                new_string=(
                    tool_input.get("content")
                    or tool_input.get("new_string")
                    or payload.get("new_string", "")
                ),
            )
        else:
            diff = ""

    try:
        result = run_pipeline(
            config_path,
            file_path,
            diff,
            rule_filter=set(args.rule) if args.rule else None,
            include_skipped=args.explain,
        )
    except ConfigError as e:
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        sys.exit(1)

    if args.explain:
        _print_explain(result, file_path)
        return

    if args.print_prompt:
        if result.get("status") == "evaluate":
            print(_build_semantic_prompt(result))
        else:
            print(
                json.dumps(
                    {
                        "note": "No semantic evaluation to print (status is not 'evaluate').",
                        "result": result,
                    },
                    indent=2,
                )
            )
        return

    print(json.dumps(result, indent=2))

    if result.get("status") == "untrusted":
        sys.stderr.write(
            _untrusted_stderr(
                result.get("config", config_path),
                result.get("trust_status", "untrusted"),
                result.get("trust_detail", ""),
            )
        )
        sys.exit(0)
    if result.get("status") == "blocked":
        sys.stderr.write(_format_blocked_stderr(result))
        sys.exit(2)


if __name__ == "__main__":
    main()
