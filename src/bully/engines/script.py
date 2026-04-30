"""Script engine: subprocess execution + capability-based env shaping."""

from __future__ import annotations

import os
import shlex
import subprocess

from bully.config.parser import Rule, Violation
from bully.engines.output import (
    FALLBACK_MAX_DESC,
    FALLBACK_MAX_VIOLATIONS,
    SEPARATOR_ONLY,
    parse_script_output,
)


def capability_env(base_env: dict[str, str], capabilities: dict | None) -> dict[str, str]:
    """Apply rule capabilities to a subprocess environment.

    Conservative implementation: stdlib only, no kernel-level sandboxing.
    The intent is declarative + best-effort:
      - network: false -> strip *_PROXY vars and set NO_PROXY=* so
        well-behaved clients use direct connections, then fail if no network
        is reachable. This is *not* a security boundary; it is a tripwire
        that turns accidental network use into immediate failure.
      - writes: cwd-only -> set HOME=cwd, TMPDIR=cwd/.bully/tmp. Tools that
        respect HOME/TMPDIR will not write outside cwd.
    """
    if not capabilities:
        return dict(base_env)
    env = dict(base_env)
    if capabilities.get("network") is False:
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env.pop(key, None)
        env["NO_PROXY"] = "*"
    writes = capabilities.get("writes")
    if writes == "cwd-only":
        cwd = os.getcwd()
        env["HOME"] = cwd
        tmp = os.path.join(cwd, ".bully", "tmp")
        os.makedirs(tmp, exist_ok=True)
        env["TMPDIR"] = tmp
    return env


def execute_script_rule(rule: Rule, file_path: str, diff: str) -> list[Violation]:
    """Run a script-engine rule against a file."""
    cmd = rule.script.replace("{file}", shlex.quote(file_path))
    try:
        # bully-disable: no-shell-true-subprocess script-engine contract; cmd is shlex.quote'd above
        result = subprocess.run(
            cmd,
            shell=True,
            input=diff,
            capture_output=True,
            text=True,
            timeout=30,
            env=capability_env(os.environ.copy(), rule.capabilities),
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

    if result.returncode == 0:
        return []

    if rule.output_mode == "passthrough":
        combined = combine_streams(result.stdout, result.stderr)
        return [
            Violation(
                rule=rule.id,
                engine="script",
                severity=rule.severity,
                line=None,
                description=tail_for_description(combined),
            )
        ]

    # Parse both streams; prefer structured (line-numbered) results over
    # unstructured tail fallbacks regardless of which stream they came from.
    # Tools mix streams inconsistently (pint -> stderr, phpstan -> stdout,
    # pest -> stdout, psalm -> stderr) so pick the higher-signal stream.
    stdout_vs = parse_script_output(rule.id, rule.severity, result.stdout)
    stderr_vs: list[Violation] = []
    if result.stderr and result.stderr.strip():
        stderr_vs = parse_script_output(rule.id, rule.severity, result.stderr)

    def _has_numbered(vs: list[Violation]) -> bool:
        return any(v.line is not None for v in vs)

    stdout_numbered = _has_numbered(stdout_vs)
    stderr_numbered = _has_numbered(stderr_vs)
    if stdout_numbered and stderr_numbered:
        return [*stdout_vs, *stderr_vs]
    if stdout_numbered:
        return stdout_vs
    if stderr_numbered:
        return stderr_vs

    # Neither stream produced numbered violations; combine tails (frequently
    # split across streams) and emit one fallback violation.
    combined = combine_streams(result.stdout, result.stderr)
    tail = tail_for_description(combined)
    if tail:
        description = f"{rule.description}: {tail}" if rule.description else tail
    else:
        description = rule.description
    return [
        Violation(
            rule=rule.id,
            engine="script",
            severity=rule.severity,
            line=None,
            description=description[:FALLBACK_MAX_DESC],
        )
    ]


def combine_streams(stdout: str, stderr: str) -> str:
    """Join stdout and stderr with a visible separator when both are non-empty."""
    parts: list[str] = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    return "\n".join(parts)


def tail_for_description(text: str) -> str:
    """Return a compact tail of tool output suitable for a Violation description.

    Keeps the last few non-empty, non-separator lines (where tool errors
    typically land) and joins them with spaces. Capped at FALLBACK_MAX_DESC.
    """
    if not text:
        return ""
    keep: list[str] = []
    for raw in text.splitlines():
        trimmed = raw.strip()
        if not trimmed or SEPARATOR_ONLY.match(trimmed):
            continue
        keep.append(trimmed)
    tail = keep[-FALLBACK_MAX_VIOLATIONS:]
    return " ".join(tail)[:FALLBACK_MAX_DESC]
