"""Mode A: run every fixture in `fixtures_dir`, append a record to history JSONL."""

from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from bully.bench.fixtures import discover_fixtures
from bully.bench.git_meta import anthropic_sdk_version, git_dirty, git_sha
from bully.bench.modes.single import run_fixture


def run_mode_a(
    *,
    fixtures_dir: Path,
    history_path: Path,
    use_api: bool = True,
    iterations: int = 5,
    skip_cold_start: bool = False,
    emit_json: bool = False,
    full: bool = False,
) -> int:
    """Run every fixture in `fixtures_dir`, append a record to `history_path`.

    When `full=True`, each fixture's semantic payload triggers a real
    Anthropic `messages.create` call so the record carries real output
    tokens (and a per-fixture `cost_usd` estimate). Costs real money.
    """
    fixtures = discover_fixtures(fixtures_dir)
    if not fixtures:
        sys.stderr.write(f"bench: no fixtures found in {fixtures_dir}\n")
        return 1

    results = []
    for fx in fixtures:
        results.append(
            run_fixture(
                fx,
                iterations=iterations,
                use_api=use_api,
                skip_cold_start=skip_cold_start,
                full=full,
            )
        )

    total_wall = sum(r["wall_ms_p50"] for r in results)
    cold_vals = [r["cold_start_ms"] for r in results if r.get("cold_start_ms") is not None]
    total_cold = sum(cold_vals) if cold_vals else None
    total_input_tokens = sum(r["tokens"]["input"] for r in results)
    total_output_tokens = sum(r["tokens"].get("output", 0) for r in results)
    total_cost_usd = sum(r["tokens"].get("cost_usd", 0.0) for r in results)
    methods = {r["tokens"]["method"] for r in results if r["tokens"]["input"]}
    aggregates = {
        "total_wall_ms_p50": total_wall,
        "total_cold_start_ms": total_cold,
        "total_input_tokens": total_input_tokens,
        "tokens_method": next(iter(methods)) if len(methods) == 1 else "mixed",
    }
    if full:
        aggregates["total_output_tokens"] = total_output_tokens
        aggregates["total_cost_usd"] = total_cost_usd
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "git_sha": git_sha(),
        "git_dirty": git_dirty(),
        "python_version": platform.python_version(),
        "anthropic_sdk_version": anthropic_sdk_version(),
        "machine": f"{platform.system().lower()}-{platform.release()}",
        "fixtures": results,
        "aggregates": aggregates,
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    if emit_json:
        print(json.dumps(record, indent=2))
    else:
        print_mode_a_summary(record)
    return 0


def print_mode_a_summary(record: dict) -> None:
    """Render a human-readable summary of a Mode A record to stdout."""
    print(f"bench run @ {record['ts']}  (sha={record['git_sha'] or '?'})")
    print(f"  python={record['python_version']}  machine={record['machine']}")
    print()
    is_full = "total_cost_usd" in record["aggregates"]
    if is_full:
        header = (
            f"  {'fixture':<32} {'wall_p50_ms':>12} {'cold_ms':>10} "
            f"{'in_tok':>8} {'out_tok':>8} {'cost_usd':>10}  method"
        )
    else:
        header = f"  {'fixture':<32} {'wall_p50_ms':>12} {'cold_ms':>10} {'tokens':>9}  method"
    print(header)
    for r in record["fixtures"]:
        cold = r.get("cold_start_ms")
        cold_str = f"{cold:.1f}" if isinstance(cold, (int, float)) else "-"
        tok = r["tokens"]
        if is_full:
            out_tok = tok.get("output", 0)
            cost = tok.get("cost_usd", 0.0)
            print(
                f"  {r['name']:<32} {r['wall_ms_p50']:>12.2f} {cold_str:>10} "
                f"{tok['input']:>8} {out_tok:>8} {cost:>10.4f}  {tok['method']}"
            )
        else:
            print(
                f"  {r['name']:<32} {r['wall_ms_p50']:>12.2f} {cold_str:>10} "
                f"{tok['input']:>9}  {tok['method']}"
            )
    agg = record["aggregates"]
    print()
    line = (
        f"  totals: wall_p50={agg['total_wall_ms_p50']:.1f}ms  "
        f"cold={agg['total_cold_start_ms'] or 0:.1f}ms  "
        f"tokens={agg['total_input_tokens']} ({agg['tokens_method']})"
    )
    if is_full:
        line += f"  output={agg['total_output_tokens']}  cost=${agg['total_cost_usd']:.4f}"
    print(line)
