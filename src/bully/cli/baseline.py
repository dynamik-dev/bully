"""`bully baseline-init`: scan repo, write current violations to `.bully/baseline.json`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bully.config.parser import ConfigError
from bully.config.skip import SKIP_PATTERNS, effective_skip_patterns, path_matches_skip
from bully.runtime.runner import run_pipeline
from bully.state.baseline import line_checksum


def cmd_baseline_init(config_path: str | None, glob: str | None) -> int:
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
        if path_matches_skip(str(candidate), extra_patterns=extra_skip):
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
            checksum = line_checksum(str(candidate), line)
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
