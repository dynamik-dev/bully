"""Scope-glob matching with recursive `**` support and rule filtering."""

from __future__ import annotations

import fnmatch
from pathlib import PurePath

from bully.config.parser import Rule


def scope_glob_matches(pattern: str, file_path: str) -> bool:
    """Match a scope glob against a file path, with recursive `**` support.

    `PurePath.match` only grew zero-or-more-segment `**` semantics in Python
    3.13; bully supports 3.10+. We split the pattern on `**` and require each
    segment to match contiguously against the path, with `**` absorbing zero
    or more intermediate path segments. Single `*` still only matches within
    one segment (via fnmatch).

    The `**` path is right-anchored: the hook always passes absolute file
    paths and rule scopes are written as repo-relative globs. We retry the
    match starting at every path-parts offset so the relative glob lines up
    with the repo-relative suffix of the absolute path.
    """
    if "**" not in pattern:
        try:
            return PurePath(file_path).match(pattern)
        except ValueError:
            return False

    path_parts = PurePath(file_path).parts
    raw_segments = pattern.split("**")
    segments: list[list[str]] = []
    for raw in raw_segments:
        trimmed = raw.strip("/")
        segments.append(trimmed.split("/") if trimmed else [])

    for start in range(len(path_parts) + 1):
        if _match_glob_segments(segments, 0, path_parts, start):
            return True
    return False


def _segment_matches(globs: list[str], parts: tuple[str, ...], start: int) -> bool:
    """True iff every glob in `globs` matches `parts[start:start+len(globs)]`."""
    if start + len(globs) > len(parts):
        return False
    return all(fnmatch.fnmatchcase(parts[start + i], g) for i, g in enumerate(globs))


def _match_glob_segments(
    segments: list[list[str]],
    seg_idx: int,
    parts: tuple[str, ...],
    part_idx: int,
) -> bool:
    """Recursively match `**`-delimited glob segments against path parts."""
    if seg_idx >= len(segments):
        return part_idx == len(parts)

    globs = segments[seg_idx]
    is_last = seg_idx == len(segments) - 1
    trailing_double_star = is_last and not globs

    if seg_idx == 0:
        if not globs:
            return _match_glob_segments(segments, seg_idx + 1, parts, part_idx)
        if not _segment_matches(globs, parts, part_idx):
            return False
        new_idx = part_idx + len(globs)
        if is_last:
            return new_idx == len(parts)
        return _match_glob_segments(segments, seg_idx + 1, parts, new_idx)

    if trailing_double_star:
        return True

    if not globs:
        return _match_glob_segments(segments, seg_idx + 1, parts, part_idx)

    end_limit = len(parts) - len(globs)
    if is_last:
        return _segment_matches(globs, parts, end_limit) if end_limit >= part_idx else False
    for try_at in range(part_idx, end_limit + 1):
        if _segment_matches(globs, parts, try_at) and _match_glob_segments(
            segments, seg_idx + 1, parts, try_at + len(globs)
        ):
            return True
    return False


def filter_rules(rules: list[Rule], file_path: str) -> list[Rule]:
    """Return rules whose scope glob(s) match the given file path."""
    return [r for r in rules if any(scope_glob_matches(g, file_path) for g in r.scope)]
