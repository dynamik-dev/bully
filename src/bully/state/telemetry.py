"""Per-project telemetry log writer (`.bully/log.jsonl`)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def telemetry_path(config_path: str) -> Path | None:
    """Return the telemetry log path if telemetry is enabled for this project."""
    project_dir = Path(config_path).resolve().parent
    tel_dir = project_dir / ".bully"
    if not tel_dir.is_dir():
        return None
    return tel_dir / "log.jsonl"


def append_telemetry(
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


def append_record(log_path: Path, record: dict) -> None:
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
