"""Stateful infra: baseline, trust gate, telemetry."""

from bully.state.baseline import (
    is_baselined,
    line_checksum,
    line_has_disable,
    load_baseline,
    parse_disable_directive,
)
from bully.state.telemetry import append_record, append_telemetry, telemetry_path
from bully.state.trust import (
    cmd_trust,
    config_checksum,
    trust_status,
    untrusted_stderr,
)

__all__ = [
    "append_record",
    "append_telemetry",
    "cmd_trust",
    "config_checksum",
    "is_baselined",
    "line_checksum",
    "line_has_disable",
    "load_baseline",
    "parse_disable_directive",
    "telemetry_path",
    "trust_status",
    "untrusted_stderr",
]
