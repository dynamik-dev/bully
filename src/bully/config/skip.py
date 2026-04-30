"""File-skip patterns: built-in defaults, user-global ignore file, project skip lists."""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePath

from bully.config.loader import _resolve_extends_target
from bully.config.parser import ConfigError, parse_single_file

# User-global ignore file: one glob per line, blank lines and `#` comments
# allowed. Loaded by `effective_skip_patterns` and merged with the built-in
# `SKIP_PATTERNS` plus anything declared in `.bully.yml`.
USER_GLOBAL_IGNORE_FILENAME = ".bully-ignore"

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


def path_matches_skip(
    file_path: str,
    extra_patterns: tuple[str, ...] | list[str] = (),
) -> bool:
    """Return True if the path matches any built-in or extra skip pattern."""
    p = PurePath(file_path)
    name = p.name
    posix = p.as_posix()
    for pat in (*SKIP_PATTERNS, *extra_patterns):
        if fnmatch.fnmatch(name, pat):
            return True
        if fnmatch.fnmatch(posix, pat):
            return True
        try:
            if p.match(pat):
                return True
        except ValueError:
            pass
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if prefix in p.parts:
                return True
    return False


def load_user_global_skips() -> list[str]:
    """Load globs from `~/.bully-ignore` (one per line, `#` comments allowed)."""
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


def collect_skip_with_extends(path: str, visited: list[str] | None = None) -> list[str]:
    """Walk a config and its extends chain, collecting `skip:` entries in order."""
    visited = visited or []
    abs_path = str(Path(path).resolve())
    if abs_path in visited:
        return []
    visited = visited + [abs_path]
    if not Path(abs_path).is_file():
        return []
    try:
        parsed = parse_single_file(abs_path)
    except ConfigError:
        return []
    out: list[str] = []
    for spec in parsed.extends:
        target = _resolve_extends_target(spec, abs_path)
        out.extend(collect_skip_with_extends(str(target), visited))
    out.extend(parsed.skip)
    return out


def effective_skip_patterns(
    config_path: str,
    *,
    include_user_global: bool = True,
) -> tuple[str, ...]:
    """Return the merged tuple of built-in + user-global + project skip globs."""
    project: list[str] = []
    if config_path and Path(config_path).is_file():
        project = collect_skip_with_extends(config_path)
    user_global = load_user_global_skips() if include_user_global else []
    return (*SKIP_PATTERNS, *user_global, *project)
