"""bully bench harness: fixture suite runner + per-config token cost analysis.

The package is organized by concern (fixtures/, timing/, dispatch/, modes/),
but this `__init__` re-exports the full public surface so callers can do
`from bully.bench import X` for any X they need without knowing which file
hosts it.
"""

from bully.bench.cli import main
from bully.bench.dispatch import (
    BENCH_INPUT_PRICE_PER_MTOK,
    BENCH_MODEL,
    BENCH_OUTPUT_PRICE_PER_MTOK,
    count_tokens,
    estimate_cost_usd,
    full_dispatch,
    import_anthropic,
    load_evaluator_system_prompt,
)
from bully.bench.fixtures import Fixture, FixtureError, discover_fixtures, load_fixture
from bully.bench.git_meta import anthropic_sdk_version, git_dirty, git_sha
from bully.bench.modes.compare import run_compare
from bully.bench.modes.mode_a import print_mode_a_summary, run_mode_a
from bully.bench.modes.mode_b import print_mode_b_report, run_mode_b, synth_diff
from bully.bench.modes.single import run_fixture
from bully.bench.timing import PhaseTimer, percentile

# Compatibility aliases for the underscored names used in the original
# single-file `pipeline/bench.py`. Drop these once tests are updated to use
# the unprefixed names.
_estimate_cost_usd = estimate_cost_usd
_import_anthropic = import_anthropic
_git_sha = git_sha
_git_dirty = git_dirty
_anthropic_sdk_version = anthropic_sdk_version
_synth_diff = synth_diff
_print_mode_a_summary = print_mode_a_summary
_print_mode_b_report = print_mode_b_report
_percentile = percentile

__all__ = [
    "BENCH_INPUT_PRICE_PER_MTOK",
    "BENCH_MODEL",
    "BENCH_OUTPUT_PRICE_PER_MTOK",
    "Fixture",
    "FixtureError",
    "PhaseTimer",
    "anthropic_sdk_version",
    "count_tokens",
    "discover_fixtures",
    "estimate_cost_usd",
    "full_dispatch",
    "git_dirty",
    "git_sha",
    "import_anthropic",
    "load_evaluator_system_prompt",
    "load_fixture",
    "main",
    "percentile",
    "print_mode_a_summary",
    "print_mode_b_report",
    "run_compare",
    "run_fixture",
    "run_mode_a",
    "run_mode_b",
    "synth_diff",
]
