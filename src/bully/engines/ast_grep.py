"""AST engine: ast-grep invocation + JSON output parsing."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import PurePath

from bully.config.parser import Rule, Violation

# ast-grep `--lang` values per file extension.
AST_LANG_BY_EXT: dict[str, str] = {
    ".ts": "ts",
    ".tsx": "tsx",
    ".js": "js",
    ".jsx": "jsx",
    ".mjs": "js",
    ".cjs": "js",
    ".py": "python",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".php": "php",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".scala": "scala",
    ".lua": "lua",
    ".html": "html",
    ".css": "css",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sh": "bash",
    ".bash": "bash",
}

AST_GREP_INSTALL_HINT = "install ast-grep: brew install ast-grep  (or: cargo install ast-grep)"


def infer_ast_language(file_path: str) -> str | None:
    """Infer the ast-grep --lang value from a file path. Returns None if unknown."""
    suffix = PurePath(file_path).suffix.lower()
    return AST_LANG_BY_EXT.get(suffix)


def ast_grep_available() -> bool:
    """Return True iff `ast-grep` is on PATH."""
    return shutil.which("ast-grep") is not None


def parse_ast_grep_json(rule_id: str, severity: str, stdout: str) -> list[Violation]:
    """Parse ast-grep's --json output into Violations.

    ast-grep emits a JSON array. Each match has `range.start.line` (0-indexed)
    and `lines` (the matched source text). An empty array means no matches.
    """
    stripped = stdout.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    violations: list[Violation] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        rng = item.get("range") or {}
        start = rng.get("start") if isinstance(rng, dict) else None
        line_i: int | None = None
        if isinstance(start, dict):
            raw_line = start.get("line")
            if isinstance(raw_line, int):
                # ast-grep line numbers are 0-indexed; convert to 1-indexed.
                line_i = raw_line + 1
        matched = item.get("lines") or item.get("text") or ""
        description = str(matched).splitlines()[0].strip() if matched else ""
        violations.append(
            Violation(
                rule=rule_id,
                engine="ast",
                severity=severity,
                line=line_i,
                description=description[:500],
            )
        )
    return violations


def execute_ast_rule(rule: Rule, file_path: str) -> list[Violation]:
    """Run an ast-engine rule against a file via ast-grep.

    Caller is responsible for checking `ast_grep_available()` beforehand and
    handling the missing-tool path. This function assumes the binary exists
    and returns [] on any execution error (conservative: don't block edits
    due to tooling failure).
    """
    lang = rule.language or infer_ast_language(file_path)
    if lang is None:
        return [
            Violation(
                rule=rule.id,
                engine="ast",
                severity=rule.severity,
                line=None,
                description=(
                    f"ast-grep: could not infer --lang from path {file_path!r}; "
                    "set `language:` on the rule"
                ),
            )
        ]

    cmd = [
        "ast-grep",
        "run",
        "--pattern",
        rule.pattern or "",
        "--lang",
        lang,
        "--json=compact",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return [
            Violation(
                rule=rule.id,
                engine="ast",
                severity=rule.severity,
                line=None,
                description=f"ast-grep timed out after 30s for pattern: {rule.pattern!r}",
            )
        ]
    except FileNotFoundError:
        return []

    if result.returncode not in (0, 1):
        stderr_tail = (result.stderr or "").strip().splitlines()[-1:]
        hint = stderr_tail[0] if stderr_tail else ""
        return [
            Violation(
                rule=rule.id,
                engine="ast",
                severity=rule.severity,
                line=None,
                description=f"ast-grep failed (exit {result.returncode}): {hint}"[:500],
            )
        ]

    return parse_ast_grep_json(rule.id, rule.severity, result.stdout)
