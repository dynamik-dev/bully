"""Rule-health analyzer.

Reads .bully/log.jsonl and produces a structured report that highlights:
- Noisy rules (violation rate above a threshold)
- Dead rules (rules in the config that never fired in the log window)
- Slow rules (mean latency above a threshold)
- Per-rule counts: fires, passes, evaluate_requested
"""

from __future__ import annotations

import argparse
import json
import statistics

from bully.config.loader import parse_config


def _read_log(log_path: str) -> list[dict]:
    # Skip corrupt or non-dict lines so a single bad record does not crash
    # `bully review` -- the very tool users reach for when telemetry is messy.
    records: list[dict] = []
    try:
        with open(log_path) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except FileNotFoundError:
        return []
    return records


def _new_bucket() -> dict:
    return {
        "fires": 0,
        "passes": 0,
        "evaluate_requested": 0,
        "skipped": 0,
        "latencies": [],
        "files": set(),
    }


def analyze(
    log_path: str,
    config_path: str,
    noisy_threshold: float = 0.5,
    slow_threshold_ms: int = 500,
) -> dict:
    """Return a structured rule-health report.

    - `noisy_threshold`: rules whose violation_rate (fires / (fires+passes))
      exceeds this value are surfaced as noisy candidates.
    - `slow_threshold_ms`: rules whose mean per-run latency exceeds this are slow.
    """
    records = _read_log(log_path)
    rules = parse_config(config_path)
    configured_ids = {r.id for r in rules}

    by_rule: dict[str, dict] = {rid: _new_bucket() for rid in configured_ids}

    for rec in records:
        rec_type = rec.get("type")
        file_ = rec.get("file", "")

        if rec_type == "semantic_verdict":
            rid = rec.get("rule")
            if rid is None:
                continue
            bucket = by_rule.setdefault(rid, _new_bucket())
            verdict = rec.get("verdict")
            if verdict == "violation":
                bucket["fires"] += 1
            elif verdict == "pass":
                bucket["passes"] += 1
            if file_:
                bucket["files"].add(file_)
            continue

        if rec_type == "semantic_skipped":
            rid = rec.get("rule")
            if rid is None:
                continue
            bucket = by_rule.setdefault(rid, _new_bucket())
            bucket["skipped"] += 1
            if file_:
                bucket["files"].add(file_)
            continue

        # Default: treat as a rule-array record (existing per-edit shape).
        for rr in rec.get("rules", []):
            rid = rr.get("id")
            if rid is None:
                continue
            bucket = by_rule.setdefault(rid, _new_bucket())
            verdict = rr.get("verdict")
            if verdict == "violation":
                bucket["fires"] += 1
            elif verdict == "pass":
                bucket["passes"] += 1
            elif verdict == "evaluate_requested":
                bucket["evaluate_requested"] += 1
            latency = rr.get("latency_ms")
            if isinstance(latency, (int, float)):
                bucket["latencies"].append(float(latency))
            if file_:
                bucket["files"].add(file_)

    dead: list[str] = []
    noisy: list[str] = []
    slow: list[str] = []
    out_by_rule: dict[str, dict] = {}

    for rid, bucket in by_rule.items():
        fires = bucket["fires"]
        passes = bucket["passes"]
        requested = bucket["evaluate_requested"]
        skipped = bucket["skipped"]
        latencies = bucket["latencies"]
        total_invocations = fires + passes + requested + skipped

        mean_latency = statistics.fmean(latencies) if latencies else 0.0
        violation_rate = fires / (fires + passes) if (fires + passes) else 0.0

        out_by_rule[rid] = {
            "fires": fires,
            "passes": passes,
            "evaluate_requested": requested,
            "skipped": skipped,
            "invocations": total_invocations,
            "files_touched": len(bucket["files"]),
            "mean_latency_ms": round(mean_latency, 1),
            "violation_rate": round(violation_rate, 3),
        }

        if total_invocations == 0 and rid in configured_ids:
            dead.append(rid)
        if (fires + passes) > 0 and violation_rate >= noisy_threshold:
            noisy.append(rid)
        if latencies and mean_latency >= slow_threshold_ms:
            slow.append(rid)

    return {
        "total_edits": len(records),
        "window": {
            "first": records[0].get("ts") if records else None,
            "last": records[-1].get("ts") if records else None,
        },
        "by_rule": out_by_rule,
        "dead": sorted(dead),
        "noisy": sorted(noisy),
        "slow": sorted(slow),
        "configured_rules": sorted(configured_ids),
    }


def format_report(report: dict) -> str:
    lines: list[str] = []
    lines.append("Rule health report")
    lines.append("=" * 18)
    lines.append(f"Total edits analyzed: {report['total_edits']}")
    window = report.get("window", {})
    if window.get("first"):
        lines.append(f"Window: {window['first']} → {window['last']}")
    lines.append("")

    def section(title: str, ids: list[str], hint: str) -> None:
        if not ids:
            return
        lines.append(f"{title} ({len(ids)}): {hint}")
        for rid in ids:
            row = report["by_rule"].get(rid, {})
            lines.append(
                f"  - {rid}  fires={row.get('fires', 0)} "
                f"passes={row.get('passes', 0)} "
                f"requested={row.get('evaluate_requested', 0)} "
                f"skipped={row.get('skipped', 0)} "
                f"rate={row.get('violation_rate', 0):.0%} "
                f"avg_ms={row.get('mean_latency_ms', 0):.0f}"
            )
        lines.append("")

    section(
        "Noisy rules",
        report["noisy"],
        "fire on most edits -- consider relaxing or splitting.",
    )
    section(
        "Dead rules",
        report["dead"],
        "never invoked in this window -- consider removing or widening scope.",
    )
    section(
        "Slow rules",
        report["slow"],
        "mean latency is high -- consider simplifying or caching.",
    )

    if report["by_rule"]:
        lines.append("All rules:")
        for rid in sorted(report["by_rule"]):
            row = report["by_rule"][rid]
            lines.append(
                f"  - {rid}  fires={row['fires']} passes={row['passes']} "
                f"requested={row['evaluate_requested']} "
                f"skipped={row['skipped']} "
                f"invocations={row['invocations']} files={row['files_touched']} "
                f"rate={row['violation_rate']:.0%} avg_ms={row['mean_latency_ms']:.0f}"
            )

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bully-analyzer",
        description="Agentic Lint rule-health analyzer.",
    )
    parser.add_argument("--log", required=True, help="Path to log.jsonl")
    parser.add_argument("--config", required=True, help="Path to .bully.yml")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of formatted text.",
    )
    parser.add_argument(
        "--noisy-threshold",
        type=float,
        default=0.5,
        help="Violation rate above which a rule is flagged as noisy (default 0.5).",
    )
    parser.add_argument(
        "--slow-threshold-ms",
        type=int,
        default=500,
        help="Mean latency (ms) above which a rule is flagged as slow (default 500).",
    )
    args = parser.parse_args()

    report = analyze(
        args.log,
        args.config,
        noisy_threshold=args.noisy_threshold,
        slow_threshold_ms=args.slow_threshold_ms,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report), end="")


if __name__ == "__main__":
    main()
