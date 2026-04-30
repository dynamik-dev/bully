"""Mode B: per-rule + diff-scaling token cost analysis for a single .bully.yml."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bully import build_semantic_payload_dict, parse_config
from bully.bench.dispatch import count_tokens, load_evaluator_system_prompt
from bully.config.parser import ConfigError


def synth_diff(added_lines: int, file_path: str = "src/synth.py") -> str:
    """Build a unified diff that adds `added_lines` new lines to a file."""
    body = "".join(f"+line_{i}\n" for i in range(added_lines))
    return f"--- a/{file_path}\n+++ b/{file_path}\n@@ -0,0 +1,{added_lines} @@\n" + body


def run_mode_b(
    *,
    config_path: Path,
    use_api: bool = True,
    emit_json: bool = False,
) -> dict | None:
    """Analyze a .bully.yml's input-token cost.

    Returns the report dict on success, None on failure (error already
    printed to stderr).
    """
    if not config_path.is_file():
        sys.stderr.write(f"bench: config not found: {config_path}\n")
        return None

    try:
        rules = parse_config(str(config_path))
    except ConfigError as e:
        sys.stderr.write(f"bench: config error: {e}\n")
        return None

    semantic_rules = [r for r in rules if r.engine == "semantic"]
    deterministic = [r for r in rules if r.engine in ("script", "ast")]
    system = load_evaluator_system_prompt()

    example_file = "src/example.py"
    floor_payload = build_semantic_payload_dict(example_file, "", [], [])["_evaluator_input"]
    if not semantic_rules:
        floor_tokens, method = 0, "n/a-no-semantic-rules"
    else:
        floor_tokens, method = count_tokens(floor_payload, system=system, use_api=use_api)

    per_rule: list[dict] = []
    for r in semantic_rules:
        payload = build_semantic_payload_dict(example_file, "", [], [r])["_evaluator_input"]
        tokens, _ = count_tokens(payload, system=system, use_api=use_api)
        per_rule.append({"id": r.id, "description": r.description, "tokens": tokens - floor_tokens})
    per_rule.sort(key=lambda x: x["tokens"], reverse=True)

    diff_scaling: list[dict] = []
    for size in (1, 10, 100, 1000):
        payload = build_semantic_payload_dict(
            example_file, synth_diff(size, example_file), [], semantic_rules
        )["_evaluator_input"]
        tokens, _ = count_tokens(payload, system=system, use_api=use_api)
        diff_scaling.append({"added_lines": size, "total_tokens": tokens})

    scopes: dict[str, int] = {}
    for r in semantic_rules:
        payload = build_semantic_payload_dict(example_file, "", [], [r])["_evaluator_input"]
        tokens, _ = count_tokens(payload, system=system, use_api=use_api)
        for glob in r.scope:
            scopes[glob] = scopes.get(glob, 0) + (tokens - floor_tokens)
    scope_rows = sorted(
        ({"scope": g, "tokens": t} for g, t in scopes.items()),
        key=lambda x: x["tokens"],
        reverse=True,
    )

    report = {
        "config": str(config_path.resolve()),
        "method": method,
        "floor_tokens": floor_tokens,
        "per_rule": per_rule,
        "diff_scaling": diff_scaling,
        "scope_groups": scope_rows,
        "deterministic_rules": [{"id": r.id, "engine": r.engine} for r in deterministic],
    }
    if emit_json:
        print(json.dumps(report, indent=2))
    else:
        print_mode_b_report(report)
    return report


def print_mode_b_report(report: dict) -> None:
    print(f"config: {report['config']}")
    print(f"method: {report['method']}")
    print()
    print(f"floor tokens (per dispatch): {report['floor_tokens']}")
    print()
    if report["per_rule"]:
        print(f"  {'rule':<30} {'tokens':>8}")
        for row in report["per_rule"]:
            print(f"  {row['id']:<30} {row['tokens']:>8}")
    else:
        print("  no semantic rules")
    print()
    print("diff scaling (all semantic rules loaded):")
    print(f"  {'added_lines':<14} {'total_tokens':>12}")
    for row in report["diff_scaling"]:
        print(f"  {row['added_lines']:<14} {row['total_tokens']:>12}")
    print()
    if report["deterministic_rules"]:
        print("deterministic rules (0 model tokens):")
        for row in report["deterministic_rules"]:
            print(f"  {row['id']} ({row['engine']})")
