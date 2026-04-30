"""Config loader: parse_config (with extends resolution) + max_workers resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from bully.config.parser import ConfigError, Rule, parse_single_file


def _resolve_extends_target(spec: str, config_path: str) -> Path:
    """Resolve an extends reference to an absolute Path."""
    config_dir = Path(config_path).resolve().parent
    p = Path(spec)
    if p.is_absolute():
        return p.resolve()
    return (config_dir / p).resolve()


def collect_config_files(path: str, visited: list[str] | None = None) -> list[Path]:
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
        parsed = parse_single_file(str(abs_path))
    except ConfigError:
        return [abs_path]
    collected: list[Path] = []
    for spec in parsed.extends:
        target = _resolve_extends_target(spec, str(abs_path))
        collected.extend(collect_config_files(str(target), visited))
    collected.append(abs_path)
    return collected


def parse_config(path: str) -> list[Rule]:
    """Parse .bully.yml into Rule objects, resolving `extends:` transitively.

    Local rules override same-id rules pulled in via extends (warn on stderr).
    Raises ConfigError on cycles, unknown keys/fields, invalid enums, etc.
    """
    return _load_with_extends(path, visited=[])


def resolve_max_workers(config_path: str) -> int:
    """Resolve concurrent-rule worker count.

    Precedence (highest first):
      1. BULLY_MAX_WORKERS env var (positive int)
      2. execution.max_workers in the top-level .bully.yml
      3. Default: min(8, os.cpu_count() or 4)
    """
    env_raw = os.environ.get("BULLY_MAX_WORKERS")
    if env_raw is not None:
        try:
            n = int(env_raw)
            if n > 0:
                return n
        except ValueError:
            pass
    try:
        parsed = parse_single_file(config_path)
        if parsed.max_workers is not None:
            return parsed.max_workers
    except ConfigError:
        pass  # parse errors surface when the caller invokes parse_config directly
    return min(8, os.cpu_count() or 4)


def _load_with_extends(path: str, visited: list[str]) -> list[Rule]:
    """Recursively load a config + its extends. Returns merged rule list."""
    abs_path = str(Path(path).resolve())
    if abs_path in visited:
        cycle = " -> ".join(visited + [abs_path])
        raise ConfigError(f"extends cycle detected: {cycle}")
    visited = visited + [abs_path]

    parsed = parse_single_file(path)

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

    for r in parsed.rules:
        if r.id in merged:
            sys.stderr.write(f"bully: rule {r.id} overridden by local config\n")
        else:
            order.append(r.id)
        merged[r.id] = r

    return [merged[rid] for rid in order]
