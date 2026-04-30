"""Two-phase pipeline orchestration: deterministic checks then semantic dispatch."""

from __future__ import annotations

import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bully.config.loader import parse_config, resolve_max_workers
from bully.config.parser import Rule, Violation
from bully.config.scope import filter_rules
from bully.config.skip import SKIP_PATTERNS, effective_skip_patterns, path_matches_skip
from bully.diff.analysis import can_match_diff
from bully.diff.context import was_write_truncated_for_path
from bully.engines.ast_grep import AST_GREP_INSTALL_HINT, ast_grep_available, execute_ast_rule
from bully.engines.script import execute_script_rule
from bully.runtime.rule_runner import RuleContext, run_rules_parallel
from bully.semantic.payload import build_semantic_payload_dict
from bully.state.baseline import load_baseline
from bully.state.telemetry import append_record, append_telemetry, telemetry_path
from bully.state.trust import trust_status


class _NoopPhaseTimer:
    """Default phase timer: every call is a no-op context manager."""

    def __call__(self, name: str):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


_NOOP_PHASE_TIMER = _NoopPhaseTimer()


def run_pipeline(
    config_path: str,
    file_path: str,
    diff: str,
    rule_filter: set[str] | None = None,
    *,
    include_skipped: bool = False,
    phase_timer=_NOOP_PHASE_TIMER,
) -> dict:
    """Full two-phase pipeline.

    Phase 1: script + AST rules (deterministic). Errors block.
    Phase 2: build semantic payload for remaining semantic rules.

    When `include_skipped=True`, the result dict gains two extra fields:
    `semantic_skipped` and `rules_evaluated`. Both are gated -- hook-mode
    output stays unchanged.
    """
    start = time.perf_counter()
    rule_records: list[dict] = []
    log_path = telemetry_path(config_path)

    # Short-circuit auto-generated files (built-in + user-global + project skip).
    with phase_timer("skip_check"):
        extra_skip = effective_skip_patterns(config_path)[len(SKIP_PATTERNS) :]
        if path_matches_skip(file_path, extra_patterns=extra_skip):
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            result = {"status": "skipped", "file": file_path, "reason": "auto-generated"}
            if log_path is not None:
                append_telemetry(log_path, file_path, "skipped", rule_records, elapsed_ms)
            return result

    # Trust gate: refuse to execute any rules from an un-reviewed config.
    with phase_timer("trust_gate"):
        status, detail = trust_status(config_path)
        if status != "trusted":
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            result = {
                "status": "untrusted",
                "file": file_path,
                "config": str(Path(config_path).resolve()),
                "trust_status": status,
                "trust_detail": detail,
            }
            if log_path is not None:
                append_telemetry(
                    log_path, file_path, f"untrusted:{status}", rule_records, elapsed_ms
                )
            return result

    with phase_timer("parse_config"):
        rules = parse_config(config_path)
    with phase_timer("filter_rules"):
        matching = filter_rules(rules, file_path)
        if rule_filter:
            matching = [r for r in matching if r.id in rule_filter]

    def flush(status: str, result: dict) -> dict:
        if log_path is not None:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            append_telemetry(log_path, file_path, status, rule_records, elapsed_ms)
        return result

    if not matching:
        return flush("pass", {"status": "pass", "file": file_path})

    script_rules = [r for r in matching if r.engine == "script"]
    ast_rules = [r for r in matching if r.engine == "ast"]
    semantic_rules = [r for r in matching if r.engine == "semantic"]

    all_violations: list[Violation] = []
    passed_checks: list[str] = []
    baseline = load_baseline(config_path)

    max_workers = resolve_max_workers(config_path)
    rule_ctx = RuleContext(
        file_path=file_path,
        diff=diff,
        baseline=baseline,
        config_path=config_path,
    )

    def _adapter_script(rule, rctx):
        return execute_script_rule(rule, rctx.file_path, rctx.diff)

    def _adapter_ast(rule, rctx):
        return execute_ast_rule(rule, rctx.file_path)

    def _fold(results):
        for result in results:
            if result.violations:
                all_violations.extend(result.violations)
            else:
                passed_checks.append(result.rule_id)
            rule_records.append(result.record)

    with phase_timer("script_exec"):
        if script_rules:
            _fold(
                run_rules_parallel(script_rules, rule_ctx, "script", _adapter_script, max_workers)
            )

    with phase_timer("ast_exec"):
        if ast_rules:
            if ast_grep_available():
                _fold(run_rules_parallel(ast_rules, rule_ctx, "ast", _adapter_ast, max_workers))
            else:
                sys.stderr.write(
                    "bully: engine:ast rules matched but ast-grep not on PATH; skipping. "
                    f"{AST_GREP_INSTALL_HINT}\n"
                )
                for rule in ast_rules:
                    rule_records.append(
                        {
                            "id": rule.id,
                            "engine": "ast",
                            "verdict": "skipped",
                            "severity": rule.severity,
                            "reason": "ast-grep-not-installed",
                        }
                    )

    # Can't-match filters for semantic rules.
    with phase_timer("semantic_build"):
        dispatched_semantic: list[Rule] = []
        semantic_skipped: list[dict] = []
        for rule in semantic_rules:
            ok, reason = can_match_diff(rule, diff)
            if ok:
                dispatched_semantic.append(rule)
                rule_records.append(
                    {
                        "id": rule.id,
                        "engine": "semantic",
                        "verdict": "evaluate_requested",
                        "severity": rule.severity,
                    }
                )
            else:
                semantic_skipped.append({"rule": rule.id, "reason": reason})
                if log_path is not None:
                    append_record(
                        log_path,
                        {
                            "ts": datetime.now(timezone.utc)
                            .isoformat(timespec="seconds")
                            .replace("+00:00", "Z"),
                            "type": "semantic_skipped",
                            "file": file_path,
                            "rule": rule.id,
                            "reason": reason,
                        },
                    )

    blocking = [v for v in all_violations if v.severity == "error"]

    def _decorate(result: dict) -> dict:
        if not include_skipped:
            return result
        result["semantic_skipped"] = list(semantic_skipped)
        result["rules_evaluated"] = explain_rules_evaluated(
            rule_records, semantic_skipped, dispatched_semantic
        )
        return result

    if blocking:
        return _decorate(
            flush(
                "blocked",
                {
                    "status": "blocked",
                    "file": file_path,
                    "violations": [asdict(v) for v in all_violations],
                    "passed": passed_checks,
                },
            )
        )

    if dispatched_semantic:
        payload = build_semantic_payload_dict(file_path, diff, passed_checks, dispatched_semantic)
        result = {"status": "evaluate", **payload}
        if was_write_truncated_for_path(file_path):
            result["write_content"] = "truncated"
        if all_violations:
            result["warnings"] = [asdict(v) for v in all_violations]
        return _decorate(flush("evaluate", result))

    result = {"status": "pass", "file": file_path, "passed": passed_checks}
    if all_violations:
        result["warnings"] = [asdict(v) for v in all_violations]
    return _decorate(flush("pass", result))


def explain_rules_evaluated(
    rule_records: list[dict],
    semantic_skipped: list[dict],
    dispatched_semantic: list[Rule],
) -> list[dict]:
    """Project the internal `rule_records` into a per-rule verdict line.

    Verdicts: `fire` (deterministic violation), `pass` (deterministic clean
    or semantic dispatched-no-violation), `skipped` (can't-match heuristic
    or ast-grep missing), `dispatched` (semantic rule sent to the evaluator).
    """
    dispatched_ids = {r.id for r in dispatched_semantic}
    out: list[dict] = []
    for rec in rule_records:
        rule_id = rec.get("id", "")
        engine = rec.get("engine", "")
        record_verdict = rec.get("verdict", "")
        if record_verdict == "violation":
            out.append({"rule": rule_id, "engine": engine, "verdict": "fire"})
        elif record_verdict == "pass":
            out.append({"rule": rule_id, "engine": engine, "verdict": "pass"})
        elif record_verdict == "evaluate_requested":
            out.append(
                {
                    "rule": rule_id,
                    "engine": engine,
                    "verdict": "dispatched" if rule_id in dispatched_ids else "pass",
                }
            )
        elif record_verdict == "skipped":
            out.append(
                {
                    "rule": rule_id,
                    "engine": engine,
                    "verdict": "skipped",
                    "reason": rec.get("reason", ""),
                }
            )
    for skip in semantic_skipped:
        out.append(
            {
                "rule": skip["rule"],
                "engine": "semantic",
                "verdict": "skipped",
                "reason": skip["reason"],
            }
        )
    return out


def print_explain(result: dict, file_path: str) -> None:
    """Render the --explain output: one line per rule in scope.

    Falls back to a clear one-liner when the result has a non-evaluating
    status (skipped, untrusted, no rules in scope) so authors aren't left
    staring at silence.
    """
    status = result.get("status", "")
    print(f"file: {file_path}")
    print(f"status: {status}")
    if status == "skipped":
        print(f"  pipeline skipped (reason: {result.get('reason', 'unknown')})")
        return
    if status == "untrusted":
        print(f"  config not trusted on this machine ({result.get('trust_detail', '')})")
        return
    rules = result.get("rules_evaluated", [])
    if not rules:
        print("  no rules matched the file's scope")
        return
    for r in rules:
        verdict = r.get("verdict", "")
        rule_id = r.get("rule", "")
        engine = r.get("engine", "")
        if verdict == "skipped":
            print(f"  [{engine}] {rule_id}: skipped ({r.get('reason', '')})")
        else:
            print(f"  [{engine}] {rule_id}: {verdict}")
