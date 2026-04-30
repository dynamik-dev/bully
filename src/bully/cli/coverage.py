"""`bully coverage`: per-file rule-scope coverage from telemetry log."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bully.config.loader import parse_config
from bully.config.scope import scope_glob_matches
from bully.state.telemetry import telemetry_path


def cmd_coverage(config_path: str | None, as_json: bool) -> int:
    """Per-file rule-scope coverage: which rules apply to each file in the log."""
    path = config_path or ".bully.yml"
    cfg_abs = Path(path).resolve()
    if not cfg_abs.is_file():
        print(f"config not found: {path}", file=sys.stderr)
        return 1
    log_path = telemetry_path(str(cfg_abs))
    rules = parse_config(str(cfg_abs))

    def rules_for(file_path: str) -> list[str]:
        matched: list[str] = []
        for r in rules:
            scopes = list(r.scope) if r.scope else ["**"]
            for pat in scopes:
                if scope_glob_matches(pat, file_path):
                    matched.append(r.id)
                    break
        return matched

    seen_files: set[str] = set()
    if log_path is not None and log_path.exists():
        with open(log_path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                file_ = rec.get("file")
                if isinstance(file_, str):
                    seen_files.add(file_)

    files_report: dict[str, dict] = {}
    for f in sorted(seen_files):
        rids = rules_for(f)
        files_report[f] = {"rules_in_scope": len(rids), "rule_ids": rids}
    uncovered = [f for f, r in files_report.items() if r["rules_in_scope"] == 0]

    summary = {
        "total_rules": len(rules),
        "files_seen": len(seen_files),
        "uncovered_files": uncovered,
        "files": files_report,
    }
    if as_json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"Coverage report: {len(rules)} rules, {len(seen_files)} files seen in telemetry.")
    if uncovered:
        print(f"\nUncovered files ({len(uncovered)}): no rules apply.")
        for f in uncovered:
            print(f"  - {f}  0 rules")
    print("\nPer-file rule scope:")
    for f, r in files_report.items():
        print(f"  - {f}  {r['rules_in_scope']} rules: {', '.join(r['rule_ids']) or '(none)'}")
    return 0


def cmd_coverage_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bully coverage")
    parser.add_argument("--config", default=".bully.yml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return cmd_coverage(args.config, args.json)
