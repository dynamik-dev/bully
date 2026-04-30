"""Bench CLI: dispatch between Mode A (fixture suite), Mode B (config), --compare."""

from __future__ import annotations

import argparse
from pathlib import Path

from bully.bench.modes.compare import run_compare
from bully.bench.modes.mode_a import run_mode_a
from bully.bench.modes.mode_b import run_mode_b


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bully bench",
        description="Measure bully's speed and input-token cost.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--config",
        help="Path to a .bully.yml; enables Mode B (config cost analysis).",
    )
    mode.add_argument(
        "--compare",
        action="store_true",
        help="Mode A only: diff the last two runs in bench/history.jsonl.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a formatted table.",
    )
    parser.add_argument(
        "--no-tokens",
        action="store_true",
        help="Skip Anthropic API call; use char-count proxy for token counts.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Mode A only: make one real Anthropic messages.create per fixture "
            "to capture real output tokens + dollar cost. Costs real money."
        ),
    )
    parser.add_argument(
        "--fixtures-dir",
        default="bench/fixtures",
        help="Directory of fixture subdirectories (default: bench/fixtures).",
    )
    parser.add_argument(
        "--history",
        default="bench/history.jsonl",
        help="Path to history JSONL (default: bench/history.jsonl).",
    )

    args = parser.parse_args(argv)

    if args.config:
        report = run_mode_b(
            config_path=Path(args.config),
            use_api=not args.no_tokens,
            emit_json=args.json,
        )
        return 0 if report is not None else 1
    if args.compare:
        return run_compare(history_path=Path(args.history))
    return run_mode_a(
        fixtures_dir=Path(args.fixtures_dir),
        history_path=Path(args.history),
        use_api=not args.no_tokens,
        emit_json=args.json,
        full=args.full,
    )


if __name__ == "__main__":
    raise SystemExit(main())
