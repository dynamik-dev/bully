"""
Agentic Lint Pipeline

Two-phase evaluation: deterministic script checks, then LLM semantic payload.
Python 3.10+ stdlib only -- no external dependencies.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    engine: str
    scope: tuple[str, ...]
    severity: str
    script: str | None = None


@dataclass(frozen=True)
class Violation:
    rule: str
    engine: str
    severity: str
    line: int | None
    description: str
    suggestion: str | None = None


def _strip_inline_comment(raw: str) -> str:
    """Remove a trailing ` # comment` while respecting quoted regions."""
    in_single = False
    in_double = False
    for i, ch in enumerate(raw):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and (i == 0 or raw[i - 1].isspace()):
            return raw[:i].rstrip()
    return raw


def _parse_scalar(raw: str) -> str:
    """Normalize a scalar value: strip inline comment, then matched outer quotes only.

    - `"foo"` -> `foo`
    - `'foo'` -> `foo`
    - `"foo` -> `"foo`   (unmatched, preserve)
    - `foo"` -> `foo"`   (unmatched, preserve)
    - `foo` -> `foo`
    """
    raw = _strip_inline_comment(raw).strip()
    if len(raw) >= 2 and ((raw[0] == '"' and raw[-1] == '"') or (raw[0] == "'" and raw[-1] == "'")):
        return raw[1:-1]
    return raw


def _parse_inline_list(raw: str) -> list[str] | None:
    """Parse `[a, b, "c"]` into a list of scalars, or return None if not a list."""
    raw = _strip_inline_comment(raw).strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    inner = raw[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    for ch in inner:
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == "," and not in_single and not in_double:
            items.append(_parse_scalar("".join(buf)))
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append(_parse_scalar("".join(buf)))
    return items


def parse_config(path: str) -> list[Rule]:
    """Parse .agentic-lint.yml into Rule objects.

    Handles our constrained YAML format:
    - Top-level `rules:` key
    - Rule IDs at 2-space indent ending with `:`
    - Fields at 4-space indent as `key: value`
    - Folded scalars (>) for multi-line values at 6+ space indent
    - Inline lists: `scope: ["*.php", "*.ts"]`
    - Inline comments (`# ...`) stripped outside quoted regions
    """
    rules: list[Rule] = []
    current_id: str | None = None
    fields: dict[str, object] = {}
    folding_key: str | None = None
    folded_lines: list[str] = []

    with open(path) as f:
        lines = f.readlines()

    for line in lines:
        raw = line.rstrip("\n")
        stripped = raw.strip()

        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip())

        if folding_key is not None:
            if indent >= 6:
                folded_lines.append(stripped)
                continue
            else:
                fields[folding_key] = " ".join(folded_lines)
                folding_key = None
                folded_lines = []

        if stripped == "rules:":
            continue

        if indent == 2 and stripped.endswith(":"):
            if current_id is not None:
                rules.append(_build_rule(current_id, fields))
            current_id = stripped[:-1]
            fields = {}
            continue

        if indent == 4 and ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value_raw = value.strip()
            if value_raw == ">":
                folding_key = key
                folded_lines = []
                continue
            as_list = _parse_inline_list(value_raw)
            if as_list is not None:
                fields[key] = as_list
            else:
                fields[key] = _parse_scalar(value_raw)

    if folding_key is not None:
        fields[folding_key] = " ".join(folded_lines)

    if current_id is not None:
        rules.append(_build_rule(current_id, fields))

    return rules


def _normalize_scope(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    if value is None:
        return ("*",)
    return (str(value),)


def _build_rule(rule_id: str, fields: dict[str, object]) -> Rule:
    script_value = fields.get("script")
    return Rule(
        id=rule_id,
        description=str(fields.get("description", "")),
        engine=str(fields.get("engine", "script")),
        scope=_normalize_scope(fields.get("scope", "*")),
        severity=str(fields.get("severity", "error")),
        script=str(script_value) if script_value is not None else None,
    )


def build_diff_context(
    tool_name: str,
    file_path: str,
    old_string: str,
    new_string: str,
    context_lines: int = 5,
) -> str:
    """Produce a diff with real file line numbers for the semantic payload.

    For Edit: synthesize the pre-edit file state by replacing the first occurrence
    of new_string with old_string in the current file, then diff both versions.
    Line numbers in the output are anchored to the actual file.

    For Write: emit the full file content prefixed with line numbers so the LLM
    can cite specific lines.

    Falls back to a minimal representation when the file is missing or the
    new_string cannot be located in the file (e.g., a subsequent edit already
    replaced it).
    """
    try:
        with open(file_path) as f:
            current = f.read()
    except OSError:
        if tool_name == "Write":
            return _line_number(new_string)
        return f"--- {file_path} (file not readable)\n+++ edit\n-{old_string}\n+{new_string}\n"

    if tool_name == "Write":
        return _line_number(current)

    # Edit path: synthesize before state
    if new_string and new_string in current:
        before = current.replace(new_string, old_string, 1)
    elif old_string and old_string in current:
        # File hasn't been updated yet or edit was no-op; still useful
        before = current
        current = current.replace(old_string, new_string, 1)
    else:
        # Can't anchor to file; return a best-effort synthetic diff
        before_lines = (old_string or "").splitlines(keepends=True) or ["\n"]
        after_lines = (new_string or "").splitlines(keepends=True) or ["\n"]
        return "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"{file_path}.before",
                tofile=f"{file_path}.after",
                n=context_lines,
            )
        )

    before_lines = before.splitlines(keepends=True)
    after_lines = current.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{file_path}.before",
            tofile=f"{file_path}.after",
            n=context_lines,
        )
    )


def _line_number(content: str) -> str:
    """Prefix each line with `NNNN:` for line-anchored evaluation."""
    lines = content.splitlines()
    width = max(3, len(str(len(lines))))
    return "\n".join(f"{i:>{width}}: {line}" for i, line in enumerate(lines, start=1))


def filter_rules(rules: list[Rule], file_path: str) -> list[Rule]:
    """Return rules whose scope glob(s) match the given file path.

    A rule's `scope` is a tuple of globs. The rule matches if any glob matches.
    Uses PurePath.match for right-anchored glob matching:
    - `*.php` matches any .php file at any depth
    - `src/*.ts` matches .ts files directly under src/
    - `*` matches everything
    """
    path = PurePath(file_path)
    return [r for r in rules if any(path.match(g) for g in r.scope)]


_FILE_LINE_COL = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$")
_FILE_LINE = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):\s*(?P<msg>.+)$")
_LINE_CONTENT = re.compile(r"^(?P<line>\d+)[:\s-]+(?P<msg>.*)$")


def _violation_from_dict(rule_id: str, severity: str, d: dict) -> Violation | None:
    line = d.get("line") or d.get("lineNumber") or d.get("line_no")
    message = d.get("message") or d.get("msg") or d.get("description") or ""
    if line is None and not message:
        return None
    try:
        line_i = int(line) if line is not None else None
    except (TypeError, ValueError):
        line_i = None
    return Violation(
        rule=rule_id,
        engine="script",
        severity=severity,
        line=line_i,
        description=str(message).strip(),
    )


def parse_script_output(rule_id: str, severity: str, output: str) -> list[Violation]:
    """Parse common tool output formats into Violation records.

    Recognized (in order of preference):
    1. JSON object or array with `line`/`message` keys
    2. `file:line:col: message` (eslint, ruff, clang, phpstan)
    3. `file:line: message` (mypy, many compilers)
    4. `line:content` (grep -n and similar)
    5. Anything else: one violation with the raw output as description

    Never drops output silently. If a script prints something, the agent sees it.
    """
    stripped = output.strip()
    if not stripped:
        return []

    # 1. Try JSON
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            v = _violation_from_dict(rule_id, severity, parsed)
            if v is not None:
                return [v]
        elif isinstance(parsed, list):
            vs = [
                _violation_from_dict(rule_id, severity, item)
                for item in parsed
                if isinstance(item, dict)
            ]
            vs = [v for v in vs if v is not None]
            if vs:
                return vs

    # 2-4. Line-by-line regex matching
    violations: list[Violation] = []
    unmatched: list[str] = []
    for line in stripped.splitlines():
        if not line.strip():
            continue
        m = _FILE_LINE_COL.match(line) or _FILE_LINE.match(line)
        if m:
            violations.append(
                Violation(
                    rule=rule_id,
                    engine="script",
                    severity=severity,
                    line=int(m.group("line")),
                    description=m.group("msg").strip(),
                )
            )
            continue
        m = _LINE_CONTENT.match(line)
        if m:
            violations.append(
                Violation(
                    rule=rule_id,
                    engine="script",
                    severity=severity,
                    line=int(m.group("line")),
                    description=m.group("msg").strip(),
                )
            )
            continue
        unmatched.append(line)

    if violations:
        return violations

    # 5. Nothing matched; surface the raw output so the agent isn't left blind.
    return [
        Violation(
            rule=rule_id,
            engine="script",
            severity=severity,
            line=None,
            description=" ".join(unmatched)[:500],
        )
    ]


def execute_script_rule(rule: Rule, file_path: str, diff: str) -> list[Violation]:
    """Run a script-engine rule against a file.

    Substitutes {file} in the script command with the actual file path.
    Passes the diff on stdin. Non-zero exit = violation.
    """
    cmd = rule.script.replace("{file}", file_path)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            input=diff,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return [
            Violation(
                rule=rule.id,
                engine="script",
                severity=rule.severity,
                line=None,
                description=f"Script timed out after 30s: {cmd}",
            )
        ]

    if result.returncode != 0:
        violations = parse_script_output(rule.id, rule.severity, result.stdout)
        if not violations:
            return [
                Violation(
                    rule=rule.id,
                    engine="script",
                    severity=rule.severity,
                    line=None,
                    description=rule.description,
                )
            ]
        return violations

    return []


def build_semantic_payload(
    file_path: str,
    diff: str,
    passed_checks: list[str],
    semantic_rules: list[Rule],
) -> dict:
    """Build the payload the LLM uses for semantic evaluation.

    Includes the diff, which script checks already passed, and the
    semantic rules (with descriptions) the LLM should evaluate.
    """
    return {
        "file": file_path,
        "diff": diff,
        "passed_checks": passed_checks,
        "evaluate": [
            {"id": r.id, "description": r.description, "severity": r.severity}
            for r in semantic_rules
        ],
    }


def _telemetry_path(config_path: str) -> Path | None:
    """Return the telemetry log path if telemetry is enabled for this project.

    Telemetry is opt-in: we only write if a `.agentic-lint/` directory already
    exists next to the config. Agents and users create the directory to turn
    logging on; removing it turns it off.
    """
    project_dir = Path(config_path).resolve().parent
    tel_dir = project_dir / ".agentic-lint"
    if not tel_dir.is_dir():
        return None
    return tel_dir / "log.jsonl"


def _append_telemetry(
    log_path: Path,
    file_path: str,
    status: str,
    rule_records: list[dict],
    latency_ms: int,
) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "file": file_path,
        "status": status,
        "latency_ms": latency_ms,
        "rules": rule_records,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # Never let telemetry failures break the hook.
        pass


def run_pipeline(
    config_path: str,
    file_path: str,
    diff: str,
    rule_filter: set[str] | None = None,
) -> dict:
    """Full two-phase pipeline.

    Phase 1: Run script rules. If any errors, return blocked status.
    Phase 2: Build semantic evaluation payload for the LLM.

    If rule_filter is provided, only rules with matching ids are evaluated.

    Writes a JSONL telemetry record to `.agentic-lint/log.jsonl` when that
    directory exists next to the config -- opt-in.

    Returns one of:
    - {"status": "pass"} -- no matching rules or all passed with no semantic rules
    - {"status": "blocked", "violations": [...], "passed": [...]} -- script errors found
    - {"status": "evaluate", "file": ..., "diff": ..., "passed_checks": [...], "evaluate": [...]}
    """
    start = time.perf_counter()
    rule_records: list[dict] = []
    rules = parse_config(config_path)
    matching = filter_rules(rules, file_path)
    if rule_filter:
        matching = [r for r in matching if r.id in rule_filter]

    log_path = _telemetry_path(config_path)

    def flush(status: str, result: dict) -> dict:
        if log_path is not None:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            _append_telemetry(log_path, file_path, status, rule_records, elapsed_ms)
        return result

    if not matching:
        return flush("pass", {"status": "pass", "file": file_path})

    script_rules = [r for r in matching if r.engine == "script"]
    semantic_rules = [r for r in matching if r.engine == "semantic"]

    all_violations: list[Violation] = []
    passed_checks: list[str] = []

    for rule in script_rules:
        rule_start = time.perf_counter()
        violations = execute_script_rule(rule, file_path, diff)
        rule_ms = int((time.perf_counter() - rule_start) * 1000)
        if violations:
            all_violations.extend(violations)
            rule_records.append(
                {
                    "id": rule.id,
                    "engine": "script",
                    "verdict": "violation",
                    "severity": rule.severity,
                    "line": violations[0].line,
                    "latency_ms": rule_ms,
                }
            )
        else:
            passed_checks.append(rule.id)
            rule_records.append(
                {
                    "id": rule.id,
                    "engine": "script",
                    "verdict": "pass",
                    "severity": rule.severity,
                    "latency_ms": rule_ms,
                }
            )

    for rule in semantic_rules:
        rule_records.append(
            {
                "id": rule.id,
                "engine": "semantic",
                "verdict": "evaluate_requested",
                "severity": rule.severity,
            }
        )

    blocking = [v for v in all_violations if v.severity == "error"]

    if blocking:
        return flush(
            "blocked",
            {
                "status": "blocked",
                "file": file_path,
                "violations": [asdict(v) for v in all_violations],
                "passed": passed_checks,
            },
        )

    if semantic_rules:
        payload = build_semantic_payload(file_path, diff, passed_checks, semantic_rules)
        result = {"status": "evaluate", **payload}
        if all_violations:
            result["warnings"] = [asdict(v) for v in all_violations]
        return flush("evaluate", result)

    result = {"status": "pass", "file": file_path, "passed": passed_checks}
    if all_violations:
        result["warnings"] = [asdict(v) for v in all_violations]
    return flush("pass", result)


def _format_blocked_stderr(result: dict) -> str:
    """Render a blocked pipeline result as agent-readable text for stderr."""
    lines = ["AGENTIC LINT -- blocked. Fix these before proceeding:", ""]
    for v in result.get("violations", []):
        line_repr = v.get("line") if v.get("line") is not None else "?"
        lines.append(f"- [{v['rule']}] line {line_repr}: {v['description']}")
    passed = result.get("passed", [])
    if passed:
        lines.append("")
        lines.append(f"Passed checks: {', '.join(passed)}")
    return "\n".join(lines) + "\n"


def _read_stdin_payload() -> dict:
    """Read stdin; if JSON, return parsed dict, else wrap as raw diff."""
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return {"diff": raw}


def _build_semantic_prompt(payload: dict) -> str:
    """Render the semantic evaluation payload as a human-readable prompt.

    Used by --print-prompt and by the agentic-lint skill documentation.
    """
    lines = [
        f"Evaluate this diff against the rules below. File: {payload.get('file', '?')}",
        "",
    ]
    passed = payload.get("passed_checks", [])
    if passed:
        lines.append(f"Already passed (do not re-evaluate): {', '.join(passed)}")
        lines.append("")
    lines.append("Rules to evaluate:")
    for r in payload.get("evaluate", []):
        lines.append(f"- [{r['id']}] ({r['severity']}): {r['description']}")
    lines.append("")
    lines.append("Diff:")
    lines.append(payload.get("diff", ""))
    lines.append("")
    lines.append(
        "For each violation: rule id, line number, description, fix suggestion. "
        "If no violations, say 'no violations' explicitly."
    )
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Agentic Lint pipeline. Runs script and semantic rules for a file.",
    )
    parser.add_argument("positional", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("--config", help="Path to .agentic-lint.yml")
    parser.add_argument("--file", dest="file_path", help="Target file to evaluate")
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        help="Evaluate only this rule id. Repeatable.",
    )
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="Print the LLM prompt text for the semantic payload instead of JSON.",
    )
    parser.add_argument(
        "--diff",
        help="Inline diff string (bypasses stdin).",
    )
    args = parser.parse_args(argv)
    # Back-compat: accept positional args (used by hook)
    if args.positional and not args.config:
        args.config = args.positional[0]
    if len(args.positional) >= 2 and not args.file_path:
        args.file_path = args.positional[1]
    return args


def main() -> None:
    args = _parse_args(sys.argv[1:])

    if not args.config or not args.file_path:
        print(
            json.dumps(
                {
                    "error": "Usage: pipeline.py --config <path> --file <path> "
                    "(or positional: pipeline.py <config> <file>)"
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = args.config
    file_path = args.file_path

    if not os.path.exists(config_path):
        print(json.dumps({"status": "pass", "file": file_path, "reason": "no config found"}))
        sys.exit(0)

    if args.diff is not None:
        diff = args.diff
    else:
        payload = _read_stdin_payload()
        if "diff" in payload:
            diff = payload["diff"]
        elif "tool_name" in payload:
            diff = build_diff_context(
                tool_name=payload.get("tool_name", ""),
                file_path=payload.get("file_path", file_path),
                old_string=payload.get("old_string", ""),
                new_string=payload.get("new_string", ""),
            )
        else:
            diff = ""

    result = run_pipeline(
        config_path,
        file_path,
        diff,
        rule_filter=set(args.rule) if args.rule else None,
    )

    if args.print_prompt:
        if result.get("status") == "evaluate":
            print(_build_semantic_prompt(result))
        else:
            print(
                json.dumps(
                    {
                        "note": "No semantic evaluation to print (status is not 'evaluate').",
                        "result": result,
                    },
                    indent=2,
                )
            )
        return

    print(json.dumps(result, indent=2))

    if result.get("status") == "blocked":
        sys.stderr.write(_format_blocked_stderr(result))
        sys.exit(2)


if __name__ == "__main__":
    main()
