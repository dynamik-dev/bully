"""Skill eval harness for bully's local skills.

Runs two kinds of evals against skills in ./skills/:

1. triggers -- did Claude consult the skill given a user query?
   Reads <skill>/evals/triggers.json (a list of {query, should_trigger}).
   Invokes `claude -p` for each query and inspects the stream for skill
   invocation markers.

2. execute -- once triggered, is the output correct?
   Reads <skill>/evals/evals.json (skill-creator schema).
   For each eval, runs `claude -p` against the prompt in a workspace seeded
   with the eval's fixture files, captures the transcript, then dispatches
   a grader run (separate `claude -p` invocation) that judges the transcript
   + outputs against the expectations[] array.

Workspace layout (skill-creator-compatible):

  bench/eval-runs/<skill>/iteration-<N>/
    eval-<id>-<slug>/
      with_skill/
        run-1/
          outputs/                  # everything the executor wrote
          transcript.md             # rendered from stream-json
          eval_metadata.json        # prompt, fixture paths, model, ts
          timing.json               # executor + grader durations
          grading.json              # grader output (skill-creator schema)
    benchmark.json                  # aggregated stats
    benchmark.md                    # human-readable summary
    triggers.json                   # triggering eval results

Stdlib-only (subprocess + json + pathlib + argparse + shutil + time).
Requires `claude` on PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Pinned models. Override via flags.
DEFAULT_EXECUTOR_MODEL = "claude-sonnet-4-6"
DEFAULT_GRADER_MODEL = "claude-opus-4-7"

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = REPO_ROOT / "bench" / "eval-runs"
GRADER_PROMPT_PATH = REPO_ROOT / "bench" / "grader_prompt.md"
QUALITY_PROMPT_PATH = REPO_ROOT / "bench" / "quality_prompt.md"


# ----- helpers -----------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s[:60] or "eval"


def _next_iteration_dir(skill_dir: Path) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        (p for p in skill_dir.iterdir() if p.is_dir() and p.name.startswith("iteration-")),
        key=lambda p: int(p.name.split("-", 1)[1]) if p.name.split("-", 1)[1].isdigit() else 0,
    )
    n = (int(existing[-1].name.split("-", 1)[1]) + 1) if existing else 1
    out = skill_dir / f"iteration-{n}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _claude_cmd(
    prompt: str,
    *,
    model: str,
    cwd: Path | None,
    extra_args: list[str] | None = None,
    timeout_s: float | None = None,
) -> tuple[str, str, int]:
    """Run `claude -p <prompt>` and return (stdout, stderr, returncode).

    Uses --output-format stream-json so we can parse tool calls. Caller
    decides on permission mode via extra_args. timeout_s kills the
    subprocess after the deadline (returncode 124 to mimic `timeout`).
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if extra_args:
        cmd.extend(extra_args)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        return (
            (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            124,
        )


def _parse_stream_events(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _render_transcript(events: list[dict[str, Any]]) -> str:
    """Best-effort markdown rendering of a stream-json conversation."""
    lines: list[str] = []
    for ev in events:
        t = ev.get("type")
        if t == "user":
            lines.append("## User\n\n" + json.dumps(ev.get("message", ""), indent=2) + "\n")
        elif t == "assistant":
            msg = ev.get("message", {})
            content = msg.get("content", [])
            for block in content if isinstance(content, list) else []:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    lines.append("## Assistant (text)\n\n" + block.get("text", "") + "\n")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    lines.append(
                        f"## Tool call: {name}\n\n```json\n" + json.dumps(inp, indent=2) + "\n```\n"
                    )
                elif btype == "thinking":
                    lines.append("## (thinking)\n\n" + block.get("thinking", "") + "\n")
        elif t == "tool_result":
            content = ev.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, indent=2)
            lines.append("## Tool result\n\n```\n" + content + "\n```\n")
        elif t == "result":
            lines.append("## Result\n\n" + json.dumps(ev, indent=2) + "\n")
    return "\n".join(lines) if lines else "(empty stream)"


def _count_tool_calls(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for block in ev.get("message", {}).get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "?")
                counts[name] = counts.get(name, 0) + 1
    return counts


def _run_conversation(
    turns: list[dict[str, Any]],
    *,
    model: str,
    cwd: Path,
    extra_args: list[str],
    timeout_s: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, list[dict[str, Any]]]:
    """Drive a multi-turn session with claude. Each turn is `{"user": str,
    "assistant_contains": [optional list of substrings]}`.

    Returns (all_events, per_turn_metadata, session_id, gate_results) where:
    - all_events: concatenated stream events across all turns
    - per_turn_metadata: list of {turn_index, elapsed_s, returncode, n_tool_calls}
    - session_id: the UUID we assigned
    - gate_results: per-turn `assistant_contains` checks
      [{turn_index, missing: [phrases not found], passed: bool}]
    """
    session_id = str(uuid.uuid4())
    all_events: list[dict[str, Any]] = []
    per_turn: list[dict[str, Any]] = []
    gate_results: list[dict[str, Any]] = []
    for i, turn in enumerate(turns):
        user_msg = turn["user"]
        session_args = ["--session-id", session_id] if i == 0 else ["--resume", session_id]
        t0 = time.perf_counter()
        stdout, stderr, rc = _claude_cmd(
            user_msg,
            model=model,
            cwd=cwd,
            extra_args=extra_args + session_args,
            timeout_s=timeout_s,
        )
        elapsed = time.perf_counter() - t0
        events = _parse_stream_events(stdout)
        all_events.extend(events)
        # Collect this turn's assistant text for gate check.
        turn_text = ""
        n_tool_calls = 0
        for ev in events:
            if ev.get("type") == "assistant":
                for b in ev.get("message", {}).get("content", []) or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        turn_text += b.get("text", "") + "\n"
                    elif b.get("type") == "tool_use":
                        n_tool_calls += 1
        per_turn.append(
            {
                "turn_index": i,
                "user_msg_preview": user_msg[:120],
                "elapsed_seconds": round(elapsed, 2),
                "returncode": rc,
                "n_tool_calls": n_tool_calls,
            }
        )
        # Gate check.
        required = turn.get("assistant_contains") or []
        if required:
            missing = [p for p in required if p.lower() not in turn_text.lower()]
            gate_results.append(
                {
                    "turn_index": i,
                    "required": required,
                    "missing": missing,
                    "passed": not missing,
                }
            )
        if rc != 0 and rc != 124:
            # Hard error; stop the conversation.
            break
    return all_events, per_turn, session_id, gate_results


def _detect_skill_invocation(events: list[dict[str, Any]], skill_name: str) -> bool:
    """Returns True if any tool call references the skill (by name match)."""
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for block in ev.get("message", {}).get("content", []) or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {})
            blob = json.dumps({"name": name, "input": inp})
            if skill_name in blob or f"plugin:{skill_name}" in blob:
                return True
    return False


# ----- triggers eval ----------------------------------------------------------


def cmd_triggers(args: argparse.Namespace) -> int:
    skill_path = Path(args.skill).resolve()
    skill_name = skill_path.name
    triggers_path = skill_path / "evals" / "triggers.json"
    if not triggers_path.exists():
        print(f"no triggers.json at {triggers_path}", file=sys.stderr)
        return 2
    cases: list[dict[str, Any]] = json.loads(triggers_path.read_text())
    iter_dir = _next_iteration_dir(RUNS_ROOT / skill_name)
    out_path = iter_dir / "triggers.json"

    results: list[dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        query = case["query"]
        expected = bool(case["should_trigger"])
        print(f"[triggers] {i}/{len(cases)}  expected={expected}  query={query!r}", flush=True)
        t0 = time.perf_counter()
        stdout, stderr, rc = _claude_cmd(
            query,
            model=args.executor_model,
            cwd=REPO_ROOT,
            # Allow only Skill so Claude either invokes the skill (signal) or
            # replies in text. AskUserQuestion is blocked to prevent hangs in -p.
            extra_args=[
                "--allowedTools",
                "Skill",
                "--disallowedTools",
                "AskUserQuestion",
            ],
            timeout_s=args.timeout_s,
        )
        elapsed = time.perf_counter() - t0
        events = _parse_stream_events(stdout)
        triggered = _detect_skill_invocation(events, skill_name)
        passed = triggered == expected
        # For debugging: capture which tool calls happened and a final-text snippet.
        tool_calls_seen: list[dict[str, Any]] = []
        final_text = ""
        for ev in events:
            if ev.get("type") == "assistant":
                for block in ev.get("message", {}).get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_calls_seen.append(
                            {
                                "name": block.get("name", ""),
                                "input_keys": list((block.get("input") or {}).keys()),
                                "input_preview": json.dumps(block.get("input") or {})[:200],
                            }
                        )
                    elif block.get("type") == "text":
                        final_text = block.get("text", "")[-400:]
        results.append(
            {
                "query": query,
                "should_trigger": expected,
                "triggered": triggered,
                "passed": passed,
                "elapsed_seconds": round(elapsed, 2),
                "claude_returncode": rc,
                "tool_calls_seen": tool_calls_seen,
                "final_assistant_text_tail": final_text,
                "stderr_tail": (stderr or "")[-400:],
            }
        )

    summary = {
        "skill_name": skill_name,
        "executor_model": args.executor_model,
        "timestamp": _now_iso(),
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "trigger_rate_when_expected": (
            sum(1 for r in results if r["should_trigger"] and r["triggered"])
            / max(1, sum(1 for r in results if r["should_trigger"]))
        ),
        "false_positive_rate": (
            sum(1 for r in results if not r["should_trigger"] and r["triggered"])
            / max(1, sum(1 for r in results if not r["should_trigger"]))
        ),
        "results": results,
    }
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {out_path}")
    print(
        f"passed {summary['passed']}/{summary['total']}  "
        f"trigger_rate={summary['trigger_rate_when_expected']:.2f}  "
        f"fpr={summary['false_positive_rate']:.2f}"
    )
    return 0 if summary["failed"] == 0 else 1


# ----- execution eval ---------------------------------------------------------


def _seed_workspace(eval_dir: Path, skill_path: Path, files: list[str]) -> Path:
    """Copy fixture files into the run's working directory.

    The fixture root mirrors the skill's layout: relative paths under
    skill_path/<...> get copied to ws/<same path>. The skill's prompts
    reference paths like 'evals/files/<scenario>/...' so the workspace
    cwd must be the skill dir.
    """
    ws = eval_dir / "with_skill" / "run-1"
    outputs = ws / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    # Mirror the skill dir structure under outputs/ so the prompt's
    # relative paths resolve. The executor's cwd will be outputs/.
    for rel in files:
        src = skill_path / rel
        dst = outputs / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not src.exists():
            print(f"  WARN fixture missing: {src}", file=sys.stderr)
            continue
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    return ws


def cmd_execute(args: argparse.Namespace) -> int:
    skill_path = Path(args.skill).resolve()
    skill_name = skill_path.name
    evals_path = skill_path / "evals" / "evals.json"
    if not evals_path.exists():
        print(f"no evals.json at {evals_path}", file=sys.stderr)
        return 2
    suite = json.loads(evals_path.read_text())
    iter_dir = _next_iteration_dir(RUNS_ROOT / skill_name)

    only = set(args.only.split(",")) if args.only else None
    summaries: list[dict[str, Any]] = []
    for ev in suite["evals"]:
        eid = ev["id"]
        if only and str(eid) not in only:
            continue
        slug = _slug(ev.get("name") or ev["prompt"])
        eval_dir = iter_dir / f"eval-{eid}-{slug}"
        eval_dir.mkdir(parents=True, exist_ok=True)
        ws = _seed_workspace(eval_dir, skill_path, ev.get("files", []))
        outputs = ws / "outputs"
        transcript_path = ws / "transcript.md"
        meta_path = ws / "eval_metadata.json"
        timing_path = ws / "timing.json"
        grading_path = ws / "grading.json"

        is_multi_turn = bool(ev.get("turns"))
        meta_path.write_text(
            json.dumps(
                {
                    "eval_id": eid,
                    "eval_name": ev.get("name"),
                    "skill_name": skill_name,
                    "prompt": ev.get("prompt"),
                    "turns": ev.get("turns"),
                    "mode": "multi-turn" if is_multi_turn else "single-turn",
                    "files": ev.get("files", []),
                    "expected_output": ev.get("expected_output"),
                    "expectations": ev["expectations"],
                    "executor_model": args.executor_model,
                    "grader_model": args.grader_model,
                    "timestamp": _now_iso(),
                },
                indent=2,
            )
        )

        print(
            f"\n[execute] eval-{eid} {slug}  ({'multi-turn' if is_multi_turn else 'single-turn'})",
            flush=True,
        )
        print(f"  workspace: {outputs}", flush=True)
        executor_extra = [
            "--permission-mode",
            args.executor_permission_mode,
            "--disallowedTools",
            "AskUserQuestion",
        ]
        t0 = time.perf_counter()
        if is_multi_turn:
            events, per_turn, session_id, gate_results = _run_conversation(
                ev["turns"],
                model=args.executor_model,
                cwd=outputs,
                extra_args=executor_extra,
                timeout_s=args.executor_timeout_s,
            )
            (ws / "session_id.txt").write_text(session_id)
            (ws / "turns.json").write_text(
                json.dumps(
                    {
                        "per_turn": per_turn,
                        "gate_results": gate_results,
                    },
                    indent=2,
                )
            )
            stdout = "\n".join(json.dumps(e) for e in events)
            stderr = ""
        else:
            stdout, stderr, rc_exec = _claude_cmd(
                ev["prompt"],
                model=args.executor_model,
                cwd=outputs,
                extra_args=executor_extra,
                timeout_s=args.executor_timeout_s,
            )
            events = _parse_stream_events(stdout)
            gate_results = []
        exec_elapsed = time.perf_counter() - t0
        transcript_path.write_text(_render_transcript(events))
        (ws / "stream.jsonl").write_text(stdout)
        if stderr:
            (ws / "executor.stderr.log").write_text(stderr)

        # Grader call. The grader is a separate `claude -p` invocation that
        # reads the transcript + outputs and writes grading.json.
        grader_prompt = (
            GRADER_PROMPT_PATH.read_text()
            + "\n\n## Run inputs\n\n"
            + json.dumps(
                {
                    "skill_name": skill_name,
                    "eval_prompt": ev.get("prompt") or [t.get("user") for t in ev.get("turns", [])],
                    "expectations": ev["expectations"],
                    "transcript_path": str(transcript_path),
                    "outputs_dir": str(outputs),
                    "grading_path": str(grading_path),
                },
                indent=2,
            )
        )
        t1 = time.perf_counter()
        g_stdout, g_stderr, g_rc = _claude_cmd(
            grader_prompt,
            model=args.grader_model,
            cwd=REPO_ROOT,
            extra_args=[
                "--permission-mode",
                "bypassPermissions",
                "--disallowedTools",
                "AskUserQuestion",
            ],
            timeout_s=args.grader_timeout_s,
        )
        grader_elapsed = time.perf_counter() - t1
        if g_stderr:
            (ws / "grader.stderr.log").write_text(g_stderr)
        (ws / "grader.stream.jsonl").write_text(g_stdout)

        # Inspect grading.json (grader is supposed to write it).
        if grading_path.exists():
            grading = json.loads(grading_path.read_text())
            pr = grading.get("summary", {}).get("pass_rate", 0.0)
            print(
                f"  graded pass_rate={pr:.2f}  exec={exec_elapsed:.1f}s  grade={grader_elapsed:.1f}s",
                flush=True,
            )
        else:
            print(
                f"  WARN no grading.json at {grading_path} -- grader may have failed (rc={g_rc})",
                flush=True,
            )
            grading = None

        # Quality grader (orthogonal-quality post-grading).
        quality_path = ws / "quality.json"
        quality_elapsed = 0.0
        quality = None
        if not args.skip_quality and grading is not None:
            skill_path = REPO_ROOT / "skills" / skill_name
            quality_prompt = (
                QUALITY_PROMPT_PATH.read_text()
                + "\n\n## Run inputs\n\n"
                + json.dumps(
                    {
                        "skill_name": skill_name,
                        "skill_path": str(skill_path),
                        "eval_prompt": ev.get("prompt")
                        or [t.get("user") for t in ev.get("turns", [])],
                        "transcript_path": str(transcript_path),
                        "outputs_dir": str(outputs),
                        "grading_path": str(grading_path),
                        "quality_path": str(quality_path),
                    },
                    indent=2,
                )
            )
            t2 = time.perf_counter()
            q_stdout, q_stderr, q_rc = _claude_cmd(
                quality_prompt,
                model=args.grader_model,
                cwd=REPO_ROOT,
                extra_args=[
                    "--permission-mode",
                    "bypassPermissions",
                    "--disallowedTools",
                    "AskUserQuestion",
                ],
                timeout_s=args.grader_timeout_s,
            )
            quality_elapsed = time.perf_counter() - t2
            (ws / "quality.stream.jsonl").write_text(q_stdout)
            if q_stderr:
                (ws / "quality.stderr.log").write_text(q_stderr)
            if quality_path.exists():
                quality = json.loads(quality_path.read_text())
                qos = quality.get("overall_score")
                print(f"  quality overall={qos}  ({quality_elapsed:.1f}s)", flush=True)
            else:
                print(f"  WARN no quality.json (rc={q_rc})", flush=True)

        timing_path.write_text(
            json.dumps(
                {
                    "executor_duration_seconds": round(exec_elapsed, 2),
                    "grader_duration_seconds": round(grader_elapsed, 2),
                    "quality_grader_duration_seconds": round(quality_elapsed, 2),
                    "total_duration_seconds": round(
                        exec_elapsed + grader_elapsed + quality_elapsed, 2
                    ),
                },
                indent=2,
            )
        )

        summaries.append(
            {
                "eval_id": eid,
                "eval_name": ev.get("name"),
                "configuration": "with_skill",
                "run_number": 1,
                "mode": "multi-turn" if is_multi_turn else "single-turn",
                "result": {
                    "pass_rate": (grading or {}).get("summary", {}).get("pass_rate"),
                    "passed": (grading or {}).get("summary", {}).get("passed"),
                    "failed": (grading or {}).get("summary", {}).get("failed"),
                    "total": (grading or {}).get("summary", {}).get("total"),
                    "time_seconds": round(exec_elapsed, 2),
                    "tool_calls": sum(_count_tool_calls(events).values()),
                    "errors": 0,
                    "quality_overall": (quality or {}).get("overall_score"),
                    "quality_scores": {
                        k: v.get("value") for k, v in (quality or {}).get("scores", {}).items()
                    },
                },
                "expectations": (grading or {}).get("expectations", []),
                "tool_calls_breakdown": _count_tool_calls(events),
                "gate_results": gate_results,
                "quality_summary": (quality or {}).get("summary"),
            }
        )

    # Aggregate across this iteration.
    benchmark = _aggregate(skill_name, args.executor_model, summaries)
    (iter_dir / "benchmark.json").write_text(json.dumps(benchmark, indent=2))
    (iter_dir / "benchmark.md").write_text(_render_benchmark_md(benchmark))
    print(f"\nwrote {iter_dir / 'benchmark.json'}")
    return 0


def _aggregate(skill_name: str, executor_model: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_config: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        by_config.setdefault(r["configuration"], []).append(r)
    summary: dict[str, Any] = {}
    for config, rs in by_config.items():
        rates = [r["result"].get("pass_rate") or 0.0 for r in rs]
        times = [r["result"].get("time_seconds") or 0.0 for r in rs]
        qualities = [
            r["result"].get("quality_overall")
            for r in rs
            if r["result"].get("quality_overall") is not None
        ]
        summary[config] = {
            "pass_rate": _stats(rates),
            "time_seconds": _stats(times),
            "quality_overall": _stats(qualities) if qualities else None,
        }
    return {
        "metadata": {
            "skill_name": skill_name,
            "executor_model": executor_model,
            "timestamp": _now_iso(),
            "evals_run": sorted({r["eval_id"] for r in runs}),
            "runs_per_configuration": 1,
        },
        "runs": runs,
        "run_summary": summary,
    }


def _stats(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"mean": 0, "stddev": 0, "min": 0, "max": 0}
    return {
        "mean": round(statistics.fmean(xs), 3),
        "stddev": round(statistics.pstdev(xs), 3) if len(xs) > 1 else 0.0,
        "min": round(min(xs), 3),
        "max": round(max(xs), 3),
    }


def _render_benchmark_md(b: dict[str, Any]) -> str:
    lines = [
        f"# Benchmark: {b['metadata']['skill_name']}",
        "",
        f"- timestamp: {b['metadata']['timestamp']}",
        f"- executor: {b['metadata']['executor_model']}",
        f"- evals: {b['metadata']['evals_run']}",
        "",
        "## Per-eval",
        "",
        "| eval_id | name | mode | pass_rate | quality | time_s | tool_calls |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in b["runs"]:
        res = r["result"]
        qos = res.get("quality_overall")
        qbits = res.get("quality_scores", {})
        qstr = (
            f"{qos}" + (f" ({','.join(f'{k[0]}={v}' for k, v in qbits.items())})" if qbits else "")
            if qos is not None
            else "-"
        )
        lines.append(
            f"| {r['eval_id']} | {r.get('eval_name', '')} | {r.get('mode', 'single-turn')} | "
            f"{res.get('pass_rate')} | {qstr} | "
            f"{res.get('time_seconds')} | {res.get('tool_calls')} |"
        )
    return "\n".join(lines) + "\n"


# ----- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--executor-model", default=DEFAULT_EXECUTOR_MODEL)
    p.add_argument("--grader-model", default=DEFAULT_GRADER_MODEL)
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("triggers", help="Run triggering eval against triggers.json")
    pt.add_argument("--skill", required=True, help="path to skill dir, e.g. skills/bully-init")
    pt.add_argument(
        "--timeout-s",
        type=float,
        default=300.0,
        help="per-query timeout (default 300s). claude -p only flushes stream-json "
        "on natural completion -- on SIGKILL we lose all events, so the timeout "
        "needs to be high enough that most queries complete on their own.",
    )
    pt.set_defaults(func=cmd_triggers)

    pe = sub.add_parser("execute", help="Run execution-quality eval and grade it")
    pe.add_argument("--skill", required=True, help="path to skill dir, e.g. skills/bully-init")
    pe.add_argument("--only", help="comma-separated eval ids to run (default: all)")
    pe.add_argument(
        "--executor-timeout-s",
        type=float,
        default=600.0,
        help="executor per-eval timeout (default 600s)",
    )
    pe.add_argument(
        "--grader-timeout-s",
        type=float,
        default=300.0,
        help="grader per-eval timeout (default 300s)",
    )
    pe.add_argument(
        "--executor-permission-mode",
        default="bypassPermissions",
        choices=["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"],
        help="permission mode for the executor (default bypassPermissions for fixture-only writes)",
    )
    pe.add_argument(
        "--skip-quality",
        action="store_true",
        help="skip the orthogonal quality grader (saves ~1 grader call per eval)",
    )
    pe.set_defaults(func=cmd_execute)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
