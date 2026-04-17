# Bully Test Bench — Design

**Date:** 2026-04-17
**Status:** Approved, ready for implementation plan
**Owner:** Chris Arter

## Goal

A local-only bench for measuring two things:

1. **Tool-level speed and input-token cost of `bully` itself**, captured per run into a versioned log so regressions and trends are visible over time.
2. **Per-config input-token cost estimate** for users who want to know what running `bully` will cost them in tokens before they adopt or extend a config.

## Non-goals

- CI integration or PR regression gates
- End-to-end cost (output tokens, real Sonnet round-trips) — input-token approximation is sufficient
- Multi-machine normalization or statistical comparison across environments
- Benchmarking of user codebases — the bench operates on self-contained fixtures or a single config file

## Two modes

### Mode A — Fixture bench (`bully bench`)

Runs a fixed suite of `(config, file_path, diff)` fixtures through `run_pipeline` and records per-phase timings + payload input-token counts. Appends one line to `bench/history.jsonl` per run.

**Purpose:** Chris's regression watch. Each commit can include a fresh bench line so trends are visible via `git log`.

### Mode B — Config cost analysis (`bully bench --config <path>`)

Given any `.bully.yml`, compute a deterministic breakdown of what that config costs in input tokens per invocation. No fixtures, no history, no repetition — this is a one-shot report.

**Purpose:** Users answering "will this config be expensive?" before adopting a rule pack or adding rules.

## CLI surface

Added to `pipeline/pipeline.py` argument parser, following existing subcommand patterns:

```
bully bench                              # Mode A, uses bench/fixtures/*.json
bully bench --config path/to/.bully.yml  # Mode B
bully bench --json                       # Emit machine-readable output on stdout
bully bench --no-tokens                  # Skip Anthropic API call, use char-count proxy
bully bench --compare                    # Mode A only: diff latest two runs in history.jsonl
```

Default invocation (`bully bench`) writes a human-readable summary to stdout and appends to `bench/history.jsonl`.

## Fixture format (Mode A)

**Location:** `bench/fixtures/<name>/`, one directory per fixture. Two files per fixture:

- `config.yml` — a real `.bully.yml` using the existing format the parser already understands
- `fixture.json` — metadata: `{name, description, file_path, edit_type, diff}`

**Why two files:** the config parser is hand-rolled for a YAML-ish format; serializing a dict back into it would require a mini-writer that's easy to get wrong. Using real `.bully.yml` files means fixtures exercise the production parser end-to-end and stay human-editable.

**Example `bench/fixtures/script-only-small-diff/config.yml`:**

```yaml
rules:
  - id: no-print
    description: Disallow print() in production code
    engine: script
    scope: "**/*.py"
    severity: error
    script: "grep -n 'print(' {file} && exit 1 || exit 0"
```

**Example `bench/fixtures/script-only-small-diff/fixture.json`:**

```json
{
  "name": "script-only-small-diff",
  "description": "One script rule firing on a small Python edit",
  "file_path": "src/app.py",
  "edit_type": "Edit",
  "diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n def main():\n+    print('hello')\n     return 0\n"
}
```

**Seed fixtures to author (~8):**

1. `script-only-small-diff` — single script rule, small diff
2. `ast-only-small-diff` — single ast rule, small diff
3. `semantic-only-small-diff` — single semantic rule, small diff
4. `mixed-engines` — script + ast + semantic, mixed engines, medium diff
5. `big-extends-chain` — config that extends 3 levels deep
6. `many-semantic-rules` — 20 semantic rules (stress-test payload size)
7. `large-diff` — 500-line diff, single semantic rule (scaling check)
8. `auto-generated-skip` — file path matches `SKIP_PATTERNS`, should short-circuit

## Harness design (`pipeline/bench.py`)

New module, stdlib-only except for the optional `anthropic` import (gated).

### Per-fixture execution (Mode A)

For each fixture:

1. **Stage config** — copy `config.yml` into a tempdir; set up an isolated `.bully/` directory so real telemetry isn't polluted.
2. **Pre-trust the staged config** — mark it trusted so the pipeline's trust gate doesn't short-circuit. Exact mechanism (writing the hash directly to the trust store, or invoking the existing trust-subcommand entry point) is an implementation-plan detail.
3. **Warm run** — one call to `run_pipeline` discarded (primes Python bytecode, filesystem cache).
4. **Timed runs** — N=5 calls. For each, wrap phases with `time.perf_counter_ns`:
   - `parse_config` (includes extends chain)
   - `effective_skip_patterns` + path-skip check
   - `filter_rules` (scope-glob matching)
   - Per-engine execution (script, ast)
   - Semantic payload build + `_can_match_diff` filter
5. **Aggregate**: median and p95 per phase across the N=5 runs.
6. **Cold-start sample** — single `subprocess.run([sys.executable, str(pipeline_py), "--hook-mode"], input=...)` for this fixture, wall-clock only. Reports the Python startup cost real hooks pay.
7. **Token count** — build the semantic payload (if any semantic rules dispatched); call `count_tokens(payload)`. Record `tokens.input` and `tokens.method` (one of `"count_tokens"`, `"proxy"`).

### Phase-timing mechanism

Rather than monkey-patching `run_pipeline`, add a thin instrumentation layer: a `BenchTimer` context manager that `pipeline.run_pipeline` accepts as an optional hook parameter. Default is None (no overhead in normal runs). Bench passes in a timer that records each phase.

This requires a small surgical change to `run_pipeline` to call into the timer at phase boundaries. The alternative — copy the pipeline loop into `bench.py` — duplicates logic and drifts. Better to take the tiny hook.

### Token counting helper

```python
def count_tokens(payload: dict, *, use_api: bool = True) -> tuple[int, str]:
    """Return (token_count, method)."""
```

- If `use_api` and `ANTHROPIC_API_KEY` env var set and `anthropic` importable:
  call `client.messages.count_tokens(model=<configured>, system=..., messages=[{"role": "user", "content": json.dumps(payload)}])` → `(n, "count_tokens")`.
- Else: `(len(json.dumps(payload)), "proxy")`.

`system` prompt is the exact bully-evaluator system prompt, loaded from `agents/bully-evaluator.md` (frontmatter stripped). Same prompt real runs use, so token counts match reality.

**Model ID:** pinned to `claude-sonnet-4-6` for now (matches today's production subagent dispatch). Exposed as a config constant at the top of `bench.py` so it can be updated alongside future model bumps without hunting through the code.

### Config cost mode (Mode B)

Given a config path:

1. Parse the config. Warn if it doesn't exist or has `ConfigError`.
2. **Floor tokens**: build a semantic payload with `evaluate: []`, empty diff, `file: "<example.ts>"`. Count tokens. This is the fixed cost of any dispatch.
3. **Per-rule marginal cost**: for each semantic rule, build a payload containing just that one rule. Report `tokens - floor` as that rule's contribution. Sort descending, output as table.
4. **Diff scaling**: synthesize added-line diffs of sizes 1, 10, 100, 1000. For each, count tokens of a payload containing all semantic rules in the config. Report a small table.
5. **Script/ast summary**: count them, note "0 model tokens; local subprocess cost only." Include per-rule latency estimates only if the config has historical data in `.bully/log.jsonl`.
6. **Scope grouping**: group semantic rules by declared `scope` globs; report per-scope totals.

Output: a formatted plain-text report. `--json` flag emits the structured data.

## History record shape (Mode A)

One line appended to `bench/history.jsonl` per run:

```json
{
  "ts": "2026-04-17T14:32:10Z",
  "git_sha": "6f66843",
  "git_dirty": false,
  "python_version": "3.12.3",
  "anthropic_sdk_version": "0.40.0",
  "machine": "darwin-23.6.0",
  "fixtures": [
    {
      "name": "script-only-small-diff",
      "wall_ms_p50": 12.3,
      "wall_ms_p95": 14.1,
      "phases_ms": {
        "parse_config": 2.1,
        "skip_check": 0.3,
        "filter_rules": 0.5,
        "script_exec": 8.2,
        "ast_exec": 0.0,
        "semantic_build": 0.1
      },
      "cold_start_ms": 58.4,
      "tokens": {"input": 0, "method": "n/a-no-semantic-rules"}
    }
  ],
  "aggregates": {
    "total_wall_ms_p50": 142.7,
    "total_cold_start_ms": 465.2,
    "total_input_tokens": 8420,
    "tokens_method": "count_tokens"
  }
}
```

Fields are flat enough to grep/awk but nested where it helps human reading. JSONL keeps one-line-per-run so `git diff` is clean.

## Dependencies

- Pipeline (`pipeline/pipeline.py`): stays stdlib-only. The phase-timer hook parameter is optional; default None is zero overhead.
- Bench (`pipeline/bench.py`): stdlib-only for mode logic. `anthropic` is an **optional** dep, imported lazily inside `count_tokens`, gated on API key presence.
- `pyproject.toml`: add an `[project.optional-dependencies] bench = ["anthropic>=0.40"]` entry. Install with `pip install -e ".[bench]"`.
- If `anthropic` isn't installed or no API key, the bench still runs, falls back to proxy, and tags the output accordingly.

## File layout

```
pipeline/bench.py                       # New — harness and both modes
pipeline/pipeline.py                    # Small edit — add optional phase-timer hook
bench/fixtures/<name>/config.yml        # New — 8 hand-authored fixtures (real .bully.yml)
bench/fixtures/<name>/fixture.json      # New — paired metadata (diff, file_path, edit_type)
bench/history.jsonl                     # New — append-only run log, committed
pipeline/tests/test_bench.py            # New — unit + integration tests
pyproject.toml                          # Edit — add optional `bench` extras
docs/superpowers/specs/2026-04-17-test-bench-design.md  # This spec
```

## Testing strategy

Unit tests in `pipeline/tests/test_bench.py`:

- `count_tokens` with mocked `anthropic` client returns correct shape
- `count_tokens` with no API key falls back to proxy
- `count_tokens` with `anthropic` not importable falls back to proxy
- Fixture loader validates shape; rejects missing fields
- Phase-timer hook records each phase correctly
- History JSONL writer produces one line per run, parseable
- Config-cost report produces expected breakdown for a fixture config
- `--compare` diffs two adjacent runs correctly

Integration tests:

- Run full Mode A against the authored fixtures; assert output has all expected keys and plausible numbers (wall_ms > 0, tokens > 0 for semantic fixtures, etc.)
- Run Mode B against `examples/rules/django.yml`; assert floor > 0, per-rule table non-empty, diff scaling monotonically increasing

Keep the bench test suite fast (under ~5s). Mock the Anthropic client — actual API calls are not part of the automated test suite.

## Failure modes and handling

| Condition | Behavior |
|---|---|
| No `ANTHROPIC_API_KEY` | Fall back to proxy, tag `tokens.method: "proxy"` |
| `anthropic` not installed | Same as above |
| `--config` path missing | Print error, exit 1 |
| `--config` has `ConfigError` | Print line-anchored error, exit 1 |
| Fixture file malformed JSON | Print path + parse error, exit 1 |
| Fixture has no matching rules | Report empty phase timings + 0 tokens, don't crash |
| `git` not available (for `git_sha`) | Record `"git_sha": null`, continue |

## Open questions resolved

- **Fixture source:** hand-authored, ~8 canonical, I'll write them.
- **Token definition:** real Anthropic `messages/count_tokens` when available; `len(json.dumps())` proxy fallback. Output tokens out of scope.
- **History format:** JSONL, one line per run, committed to repo at `bench/history.jsonl`.
- **Granularity:** per-fixture row with phase breakdown inside, plus aggregates.
- **Repetition:** N=5 per fixture, median + p95 reported. One discarded warm run. One subprocess sample for cold-start.
- **Output tokens:** deferred. Input-token approximation is sufficient for the current goal.

## Future extensions (explicitly not in scope now)

- `--full` flag: real Sonnet round-trip for output-token calibration
- CI integration / regression gates
- Normalized "relative to baseline" comparison view
- Historical trend plot rendered to SVG
