# Parallelize script and AST rule execution

**Status:** Design
**Date:** 2026-04-21
**Scope:** `pipeline/pipeline.py`, new `pipeline/rule_runner.py`, config loader, tests

## Motivation

The PostToolUse hook blocks the user until every rule finishes. Script rules run serially in `pipeline.py:1735-1741` — each is a `subprocess.run` call with a 30s timeout. AST rules at `pipeline.py:1743-1751` are the same shape. Both phases are I/O-bound (subprocess waits dominate), so threading wins without the GIL being an obstacle.

Primary target: the hook path (single file × N rules, latency-sensitive). Secondary benefit: `bully check` in CI (same mechanism, more throughput).

## Non-goals

- Combining N AST rules into a single `ast-grep scan -c sgconfig.yml` invocation. That's a larger win, tracked separately.
- Parallelizing across files in `bully check`. Out of scope for this pass.
- Any change to semantic-rule dispatch or agent payload shape.
- A "compiled bash" artifact. Investigated and rejected — see conversation context; the wins don't justify the cost.

## Architecture

Extract rule execution into a new helper module, `pipeline/rule_runner.py`. `pipeline.py` is 2660 lines today; growing it further to add concurrency control pushes in the wrong direction. The new module owns three things and nothing else:

1. Two dataclasses — `RuleContext` (inputs bundled for thread passing) and `RuleResult` (outputs bundled for order-preserving fold).
2. `evaluate_rule(rule, ctx, engine, executor_fn) -> RuleResult` — the pure, thread-safe per-rule unit.
3. `run_rules_parallel(rules, ctx, engine, executor_fn, max_workers) -> list[RuleResult]` — a `ThreadPoolExecutor` driver that returns results in rule-declaration order.

`pipeline.py` keeps orchestration, config loading, diff handling, semantic-rule payload building, and the fold that merges `RuleResult`s into `all_violations` / `passed_checks` / `rule_records`.

**Boundary test:** nothing in `rule_runner.py` touches global state, the baseline file, or the telemetry log. Those stay on the main thread, where ordering is deterministic.

## Components

```python
# pipeline/rule_runner.py

@dataclass(frozen=True)
class RuleContext:
    file_path: str
    diff: str
    baseline: Baseline            # existing type from pipeline.py
    config_path: str | None

@dataclass
class RuleResult:
    rule_id: str
    violations: list[Violation]   # already filtered for line-disable + baseline
    record: dict                  # the rule_records entry
    internal_error: bool = False  # True iff evaluate_rule caught an exception

def evaluate_rule(
    rule: Rule,
    ctx: RuleContext,
    engine: str,                  # "script" | "ast"
    executor_fn: Callable[[Rule, RuleContext], list[Violation]],
) -> RuleResult: ...

def run_rules_parallel(
    rules: list[Rule],
    ctx: RuleContext,
    engine: str,
    executor_fn: Callable[[Rule, RuleContext], list[Violation]],
    max_workers: int,
) -> list[RuleResult]: ...
```

`executor_fn` is the injection point. `pipeline.py` passes thin adapters that call the existing `execute_script_rule(rule, file_path, diff)` and `execute_ast_rule(rule, file_path)`. Those functions do not change.

## Config

New top-level key in `.bully.yml`:

```yaml
execution:
  max_workers: 4   # optional; null or unset = auto
```

Resolution order (highest precedence first):

1. `BULLY_MAX_WORKERS` env var (if set and parseable as a positive int)
2. `execution.max_workers` from `.bully.yml`
3. Default: `min(8, os.cpu_count() or 4)`

Validation at config-load time: `max_workers <= 0` raises with a message naming the field. Non-integer values raise with the same message.

## Data flow

Per file:

1. `ctx = RuleContext(file_path, diff, baseline, config_path)` — built once.
2. `script_exec` phase (timed by the existing `phase_timer`):
   `results = run_rules_parallel(script_rules, ctx, "script", _adapter_script, max_workers)`
   where `_adapter_script = lambda rule, ctx: execute_script_rule(rule, ctx.file_path, ctx.diff)` — a one-line adapter that maps from `(rule, ctx)` to the existing function signatures.
3. `ast_exec` phase: same pattern with `_adapter_ast = lambda rule, ctx: execute_ast_rule(rule, ctx.file_path)`, guarded by `ast_grep_available()`. The existing "ast-grep not installed" branch (`pipeline.py:1752-1766`) is unchanged — it emits skipped records on the main thread.
4. Main thread folds each `RuleResult` in list order into `all_violations`, `passed_checks`, `rule_records`.

**Ordering guarantee:** `run_rules_parallel` submits all futures, then iterates the future list in submission order and calls `.result()`. Completion order doesn't matter; collection is strictly by index. `all_violations` and `rule_records` end up byte-identical to the serial version when no internal errors occur.

## Error handling

**Inside `evaluate_rule`:**

- `executor_fn` may return a list of `Violation`s (including timeouts, which `execute_script_rule` / `execute_ast_rule` already convert internally — those paths are unchanged).
- Any `Exception` raised by `executor_fn`, the disable filter, or the baseline filter is caught. The handler synthesizes:

  ```python
  Violation(
      rule=rule.id,
      engine=engine,
      severity="error",   # always blocking, regardless of rule.severity
      line=None,
      description=f"internal error: {type(exc).__name__}: {exc}"[:500],
  )
  ```

  and returns a `RuleResult` with `internal_error=True` and a `record` whose verdict is `"violation"` plus an `error: true` telemetry field.
- `KeyboardInterrupt` and `SystemExit` are not caught — Ctrl-C still works.

**Inside `run_rules_parallel`:** `evaluate_rule` is designed not to raise, but the pool driver wraps `future.result()` in try/except and synthesizes the same internal-error `RuleResult` if anything slips through. No future is cancelled on failure — all rules run to completion so the user sees the full picture.

**Timeouts:** already handled inside `execute_script_rule` / `execute_ast_rule` (30s per rule, emitted as a `Violation`). Parallelism doesn't change that; the pool waits as long as the slowest rule.

**Alignment with "don't let bad code land":** internal errors are emitted at `severity="error"` — blocking. If rule #3 crashes, the user gets a blocking violation for rule #3 *and* real results for the other rules. Today they'd get only rule #3's crash. Per-rule isolation strictly strengthens enforcement.

## Testing

### Unit tests — `evaluate_rule`

Location: `pipeline/tests/test_rule_runner.py` (new file).

- Passing rule → empty violations, `record["verdict"] == "pass"`.
- Rule returning violations → populated violations, `record["verdict"] == "violation"`.
- `executor_fn` raises `RuntimeError` → single `Violation` with `severity="error"`, description starts with `"internal error:"`, `internal_error=True`, `record["error"] is True`, `record["verdict"] == "violation"`.
- Line-disable comment filters a violation out.
- Baseline filters a violation out.
- `fix_hint` propagated to `Violation.suggestion` (parity with the current `_run_deterministic`).

### Unit tests — `run_rules_parallel`

- 4 rules with staggered artificial delays (injected via a test `executor_fn`) → `len(results) == 4`, `[r.rule_id for r in results]` matches input order.
- Wall time < sum of individual delays (guards against accidental serialization).
- `max_workers=1` forces serialization; `max_workers=4` runs concurrently — verified with a shared counter observing peak parallelism.
- One rule raising does not stop the others; raising rule gets an internal-error result, others get normal results.

### Config tests

- Default when `execution.max_workers` unset → `min(8, os.cpu_count() or 4)`.
- `.bully.yml` sets `execution.max_workers: 2` → pool uses 2.
- `BULLY_MAX_WORKERS=16` overrides config.
- `execution.max_workers: 0` or negative → config-load error with message naming the field.

### Integration test — hook path

- Fixture file with 4 script rules whose shell scripts each `sleep 0.3` and exit 0 → hook wall time well under 1.2s (serial would be ~1.2s; parallel ~0.3s + overhead).
- `rule_records` order matches rule declaration order.
- Agent payload byte-identical (or minimally different — only new `error` telemetry field) to the serial version on a fixture with no internal errors.

## Rollout

- Land without a feature flag. Default `max_workers = min(8, cpu_count or 4)` is safe: current serial behavior is `max_workers=1` equivalent, and users hitting trouble can set `execution.max_workers: 1` to restore exactly-serial.
- `pipeline/bench.py` already exists — add a before/after comparison on a fixture with 8+ script rules; capture numbers for the release notes.
- Update README and any user-facing docs to mention `execution.max_workers` and `BULLY_MAX_WORKERS`.
- No migration: `.bully.yml` files without an `execution:` block continue to work unchanged.

## Risks

- **User script side effects.** A small number of user script rules may rely on being the only process touching a resource (e.g., a lock file). Mitigation: `execution.max_workers: 1` restores serial execution. Document it.
- **CPU oversubscription on CI runners.** Unlikely in practice (N-per-file is small), but if a user's CI box is tiny, the default of 8 workers could cause contention. Mitigation: `BULLY_MAX_WORKERS` env var — trivial to set globally in a CI workflow.
- **Exception-isolation masking real bugs.** Converting exceptions to violations could hide a broken rule as "just another violation." Mitigation: the `error: true` telemetry field makes internal errors queryable in the log; the blocking severity forces the user to notice.
