"""Script-output parsing: tries JSON first, then per-line regex with continuation joining."""

from __future__ import annotations

import json
import re

from bully.config.parser import Violation

_FILE_LINE_COL = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$")
_FILE_LINE = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):\s*(?P<msg>.+)$")
_LINE_CONTENT = re.compile(r"^(?P<line>\d+)[:\s-]+(?P<msg>.*)$")
# Rows of `-`, `=`, `_`, `*`, `|`, `+` with optional whitespace are table
# separators emitted by phpstan, pest, psalm, and similar reporters. They
# carry no semantic content and pollute the fallback blob.
SEPARATOR_ONLY = re.compile(r"^[\s\-=_*|+]+$")

FALLBACK_MAX_DESC = 500
FALLBACK_MAX_VIOLATIONS = 20


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
    """Parse common tool output formats into Violation records.

    Strategy (ordered):
    1. JSON (object or array) -> structured dict parsing.
    2. Per-line regex scan with stateful continuation-joining. A line whose
       trimmed form matches `FILE:LINE:COL`, `FILE:LINE`, or leading
       `LINE` opens a new violation; subsequent non-matching, non-separator
       lines concatenate onto that violation's description. Table separator
       rows (`------`, `======`) are dropped.
    3. Fallback: when no numbered lines matched at all, return up to
       FALLBACK_MAX_VIOLATIONS individual violations for the *tail* of
       unmatched lines (errors typically land at the end of tool output).
    """
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
    current: Violation | None = None
    current_parts: list[str] = []

    def _flush_current() -> None:
        nonlocal current, current_parts
        if current is not None:
            joined = " ".join(p.strip() for p in current_parts if p.strip())
            current.description = joined[:FALLBACK_MAX_DESC]
            violations.append(current)
        current = None
        current_parts = []

    for raw in stripped.splitlines():
        trimmed = raw.lstrip()
        if not trimmed:
            _flush_current()
            continue
        if SEPARATOR_ONLY.match(trimmed):
            _flush_current()
            continue

        m = _FILE_LINE_COL.match(trimmed) or _FILE_LINE.match(trimmed)
        if m:
            _flush_current()
            current = Violation(
                rule=rule_id,
                engine="script",
                severity=severity,
                line=int(m.group("line")),
                description="",
            )
            current_parts = [m.group("msg").strip()]
            continue

        m = _LINE_CONTENT.match(trimmed)
        if m:
            _flush_current()
            current = Violation(
                rule=rule_id,
                engine="script",
                severity=severity,
                line=int(m.group("line")),
                description="",
            )
            current_parts = [m.group("msg").strip()]
            continue

        if current is not None:
            current_parts.append(trimmed)
        else:
            unmatched.append(trimmed)

    _flush_current()

    if violations:
        return violations

    if not unmatched:
        return []
    tail = unmatched[-FALLBACK_MAX_VIOLATIONS:]
    return [
        Violation(
            rule=rule_id,
            engine="script",
            severity=severity,
            line=None,
            description=line[:FALLBACK_MAX_DESC],
        )
        for line in tail
    ]
