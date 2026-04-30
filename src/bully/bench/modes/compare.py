"""`bench --compare`: deltas between the last two history records."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_compare(*, history_path: Path) -> int:
    """Print deltas between the last two runs in history_path."""
    if not history_path.is_file():
        sys.stderr.write(f"bench: history file not found: {history_path}\n")
        return 1
    lines = [
        json.loads(line)
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) < 2:
        sys.stderr.write("bench: --compare needs at least two runs in history\n")
        return 1

    older, newer = lines[-2], lines[-1]
    print(f"comparing {older.get('git_sha') or '?'} -> {newer.get('git_sha') or '?'}")
    print(f"  {older['ts']}  ->  {newer['ts']}")
    print()

    fx_by_name = {f["name"]: f for f in newer.get("fixtures", [])}
    old_by_name = {f["name"]: f for f in older.get("fixtures", [])}
    all_names = sorted(set(fx_by_name) | set(old_by_name))

    print(f"  {'fixture':<32} {'wall_ms delta':>14} {'tokens delta':>14}")
    for name in all_names:
        new_fx = fx_by_name.get(name, {})
        old_fx = old_by_name.get(name, {})
        dw = new_fx.get("wall_ms_p50", 0) - old_fx.get("wall_ms_p50", 0)
        dt = new_fx.get("tokens", {}).get("input", 0) - old_fx.get("tokens", {}).get("input", 0)
        sign_w = "+" if dw >= 0 else ""
        sign_t = "+" if dt >= 0 else ""
        print(f"  {name:<32} {sign_w}{dw:>13.2f} {sign_t}{dt:>13}")

    agg_new = newer.get("aggregates", {})
    agg_old = older.get("aggregates", {})
    dw_tot = agg_new.get("total_wall_ms_p50", 0) - agg_old.get("total_wall_ms_p50", 0)
    dt_tot = agg_new.get("total_input_tokens", 0) - agg_old.get("total_input_tokens", 0)
    print()
    print(f"  totals: wall_delta={dw_tot:+.2f}ms  tokens_delta={dt_tot:+}")
    return 0
