"""Baseline grandfathering + per-line `bully-disable:` directive parsing."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_DISABLE_RE = re.compile(r"bully-disable\s*:?\s*(?P<ids>[^#\n\r]*?)(?:\s+(?P<reason>[^#\n\r]+))?$")


def baseline_path(config_path: str) -> Path:
    return Path(config_path).resolve().parent / ".bully" / "baseline.json"


def load_baseline(config_path: str) -> dict:
    p = baseline_path(config_path)
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


def line_checksum(file_path: str, line: int | None) -> str:
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


def is_baselined(
    baseline: dict, rule_id: str, config_path: str, file_path: str, line: int | None
) -> bool:
    if not baseline or line is None:
        return False
    try:
        rel = str(Path(file_path).resolve().relative_to(Path(config_path).resolve().parent))
    except ValueError:
        rel = file_path
    checksum = line_checksum(file_path, line)
    if not checksum:
        return False
    return (rule_id, rel, line, checksum) in baseline


def parse_disable_directive(text: str) -> tuple[set[str] | None, str | None]:
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


def line_has_disable(file_path: str, line: int | None, rule_id: str) -> bool:
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
        ids, _reason = parse_disable_directive(text)
        if ids is None:
            continue
        if not ids or rule_id in ids:
            return True
    return False
