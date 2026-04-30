"""Per-fixture run: warm + N timed iterations + cold-start subprocess + token count."""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from bully import build_semantic_payload_dict, filter_rules, parse_config, run_pipeline
from bully.bench.dispatch import (
    count_tokens,
    estimate_cost_usd,
    full_dispatch,
    load_evaluator_system_prompt,
)
from bully.bench.fixtures import Fixture
from bully.bench.timing import PhaseTimer, percentile


def run_fixture(
    fx: Fixture,
    *,
    iterations: int = 5,
    use_api: bool = True,
    skip_cold_start: bool = False,
    full: bool = False,
) -> dict:
    """Run one fixture: warm + N timed + cold-start + token count.

    Returns a per-fixture result dict suitable for the history JSONL.

    When `full=True` and the Anthropic SDK + API key are available, makes
    one real `messages.create` round-trip per fixture to capture real
    output tokens. Costs actual model spend -- use for calibration runs.
    """
    cfg_path = str(fx.config_path)

    # Bundled fixtures are trusted by construction; short-circuit the trust
    # gate so the bench doesn't require `bully trust` on every fixture
    # config. Save-and-restore so this doesn't leak to callers that import
    # the bench as a library.
    prior_trust = os.environ.get("BULLY_TRUST_ALL")
    os.environ["BULLY_TRUST_ALL"] = "1"
    try:
        # Warm run (discarded).
        run_pipeline(cfg_path, fx.file_path, fx.diff)

        # Timed runs.
        wall_samples_ns: list[int] = []
        phase_samples_ns: dict[str, list[int]] = {}
        for _ in range(iterations):
            pt = PhaseTimer()
            t0 = time.perf_counter_ns()
            run_pipeline(cfg_path, fx.file_path, fx.diff, phase_timer=pt)
            wall_samples_ns.append(time.perf_counter_ns() - t0)
            for name, samples in pt.results_ns().items():
                # Sum of this phase for this iteration (phases may re-enter).
                phase_samples_ns.setdefault(name, []).append(sum(samples))

        wall_ms = [ns / 1_000_000 for ns in wall_samples_ns]
        phases_ms = {
            name: statistics.median([ns / 1_000_000 for ns in samples])
            for name, samples in phase_samples_ns.items()
        }

        # Cold-start: one subprocess invocation, wall-clock only. Use the
        # default CLI path (not --hook-mode) so the subprocess doesn't block
        # waiting on a Claude Code tool-hook payload.
        cold_start_ms: float | None = None
        if not skip_cold_start:
            t0 = time.perf_counter_ns()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bully",
                    "--config",
                    cfg_path,
                    "--file",
                    fx.file_path,
                    "--diff",
                    fx.diff,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PYTHONPATH": _src_path()},
            )
            cold_start_ms = (time.perf_counter_ns() - t0) / 1_000_000

        # Tokens: build the real semantic payload and count. Short-circuit
        # when no semantic rules match -- a real run wouldn't dispatch at all.
        rules = parse_config(cfg_path)
        matching = filter_rules(rules, fx.file_path)
        passed = [r.id for r in matching if r.engine in ("script", "ast")]
        semantic = [r for r in matching if r.engine == "semantic"]
        if semantic:
            system = load_evaluator_system_prompt()
            payload = build_semantic_payload_dict(fx.file_path, fx.diff, passed, semantic)
            if full:
                input_tokens, output_tokens, method = full_dispatch(
                    payload["_evaluator_input"], system=system
                )
                tokens_record = {
                    "input": input_tokens,
                    "output": output_tokens,
                    "method": method,
                    "cost_usd": estimate_cost_usd(input_tokens, output_tokens),
                }
            else:
                input_tokens, method = count_tokens(
                    payload["_evaluator_input"], system=system, use_api=use_api
                )
                tokens_record = {"input": input_tokens, "method": method}
        else:
            tokens_record = {"input": 0, "method": "n/a-no-semantic-rules"}

        return {
            "name": fx.name,
            "description": fx.description,
            "wall_ms_p50": statistics.median(wall_ms),
            "wall_ms_p95": percentile(wall_ms, 95),
            "phases_ms": phases_ms,
            "cold_start_ms": cold_start_ms,
            "tokens": tokens_record,
        }
    finally:
        if prior_trust is None:
            os.environ.pop("BULLY_TRUST_ALL", None)
        else:
            os.environ["BULLY_TRUST_ALL"] = prior_trust


def _src_path() -> str:
    """Path that puts `bully` on sys.path for a subprocess invocation.

    Editable installs already place `bully` on sys.path; this fallback lets
    the bench run cold-start measurements against a checkout that hasn't
    been `pip install`'d.
    """
    import bully  # noqa: PLC0415

    pkg_dir = Path(bully.__file__).resolve().parent
    return str(pkg_dir.parent)
