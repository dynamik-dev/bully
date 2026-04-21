"""Per-rule execution helpers for pipeline.py.

Extracted from the pipeline.py `_run_deterministic` closure so rule
evaluation can be parallelized via a ThreadPoolExecutor while keeping
the main-thread fold (violation/record collection) deterministic.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

# Dual-mode import: works both when `pipeline/` is on sys.path (test / direct
# `./bully` entry) AND when `pipeline` is imported as a proper package (e.g.
# via the installed `bully` console script, which resolves to
# `pipeline.pipeline:main`). The fallback preserves the historical test
# convention without breaking installed execution.
try:
    from pipeline.pipeline import Rule, Violation, _is_baselined, _line_has_disable
except ImportError:
    from pipeline import Rule, Violation, _is_baselined, _line_has_disable


@dataclass(frozen=True)
class RuleContext:
    """Per-file context passed to every rule evaluator.

    Intentionally frozen + immutable so it is safe to share across worker
    threads. Nothing inside should be mutated.
    """

    file_path: str
    diff: str
    baseline: dict  # keys are (rule_id, rel_path, line, checksum) tuples
    config_path: str | None


@dataclass
class RuleResult:
    """Output of a single rule evaluation, ready for main-thread fold."""

    rule_id: str
    violations: list  # list[Violation] — typed loosely to avoid import cycle
    record: dict
    internal_error: bool = False


def evaluate_rule(
    rule: Rule,
    ctx: RuleContext,
    engine: str,
    executor_fn: Callable[[Rule, RuleContext], list[Violation]],
) -> RuleResult:
    """Run one rule against one file and return a ready-to-fold RuleResult.

    Exceptions raised by executor_fn or the filters are caught and
    converted to a single blocking Violation so one bad rule cannot
    take down the rest of the run. KeyboardInterrupt and SystemExit
    are intentionally not caught.
    """
    start = time.perf_counter()
    try:
        violations = executor_fn(rule, ctx)

        if rule.fix_hint:
            violations = [replace(v, suggestion=v.suggestion or rule.fix_hint) for v in violations]

        filtered: list[Violation] = []
        for v in violations:
            if _line_has_disable(ctx.file_path, v.line, rule.id):
                continue
            if _is_baselined(ctx.baseline, rule.id, ctx.config_path, ctx.file_path, v.line):
                continue
            filtered.append(v)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 — intentional broad catch for isolation
        latency_ms = int((time.perf_counter() - start) * 1000)
        description = f"internal error: {type(exc).__name__}: {exc}"[:500]
        err_violation = Violation(
            rule=rule.id,
            engine=engine,
            severity="error",
            line=None,
            description=description,
        )
        record = {
            "id": rule.id,
            "engine": engine,
            "verdict": "violation",
            "severity": "error",
            "line": None,
            "latency_ms": latency_ms,
            "error": True,
        }
        return RuleResult(
            rule_id=rule.id,
            violations=[err_violation],
            record=record,
            internal_error=True,
        )

    latency_ms = int((time.perf_counter() - start) * 1000)
    if filtered:
        record = {
            "id": rule.id,
            "engine": engine,
            "verdict": "violation",
            "severity": rule.severity,
            "line": filtered[0].line,
            "latency_ms": latency_ms,
        }
    else:
        record = {
            "id": rule.id,
            "engine": engine,
            "verdict": "pass",
            "severity": rule.severity,
            "latency_ms": latency_ms,
        }

    return RuleResult(rule_id=rule.id, violations=filtered, record=record)


def run_rules_parallel(
    rules: list[Rule],
    ctx: RuleContext,
    engine: str,
    executor_fn: Callable[[Rule, RuleContext], list[Violation]],
    max_workers: int,
) -> list[RuleResult]:
    """Evaluate `rules` concurrently and return RuleResults in submission order.

    `evaluate_rule` is designed not to raise, but we still wrap future.result()
    in a best-effort try/except that synthesizes an internal-error RuleResult
    if something slips through (e.g. a buggy executor_fn somehow bypasses
    the inner guard). No future is cancelled on failure — every rule runs to
    completion so the user sees the full picture.
    """
    if not rules:
        return []
    # Fast path: a single rule doesn't benefit from pool setup (~1-2ms of
    # thread-spawn and executor teardown overhead per invocation). Call
    # evaluate_rule inline. This matters because many real-world files match
    # only one or two rules, and the hook runs per-file.
    if len(rules) == 1:
        return [evaluate_rule(rules[0], ctx, engine, executor_fn)]
    # Imported lazily: most hook invocations hit the single-rule fast path
    # above and never need concurrent.futures, which is slow to import cold.
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    # A pool with zero workers would deadlock on submit(); clamp to >=1.
    workers = max(1, min(max_workers, len(rules)))
    results: list[RuleResult | None] = [None] * len(rules)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bully-rule") as pool:
        futures = [pool.submit(evaluate_rule, rule, ctx, engine, executor_fn) for rule in rules]
        for idx, fut in enumerate(futures):
            try:
                results[idx] = fut.result()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 — defence-in-depth
                rule = rules[idx]
                description = f"internal error: {type(exc).__name__}: {exc}"[:500]
                err_violation = Violation(
                    rule=rule.id,
                    engine=engine,
                    severity="error",
                    line=None,
                    description=description,
                )
                record = {
                    "id": rule.id,
                    "engine": engine,
                    "verdict": "violation",
                    "severity": "error",
                    "line": None,
                    "latency_ms": None,
                    "error": True,
                }
                results[idx] = RuleResult(
                    rule_id=rule.id,
                    violations=[err_violation],
                    record=record,
                    internal_error=True,
                )
    return [r for r in results if r is not None]
