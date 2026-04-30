"""Tests for TASK-1.3: surfacing semantic skip reasons via --explain.

Authors today have no way to tell whether a semantic rule "didn't fire because
it passed" vs "didn't fire because the can't-match heuristics dropped it."
This test suite covers the two-part fix:

1. `run_pipeline(..., include_skipped=True)` adds `semantic_skipped` and
   `rules_evaluated` to the result dict (gated so hook-mode output is
   unchanged).
2. The `--explain` CLI flag prints a per-rule verdict line
   (`fire` / `pass` / `skipped <reason>`) for every rule in scope.
"""

import json
import subprocess
import sys
from pathlib import Path

from bully import run_pipeline  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


# ---- run_pipeline include_skipped flag ----


def _write_semantic_only_config(tmp_path: Path) -> Path:
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "schema_version: 1\n"
        "rules:\n"
        "  prose-rule:\n"
        '    description: "no jargon in committed source"\n'
        "    engine: semantic\n"
        '    scope: ["*.py"]\n'
        "    severity: warning\n"
    )
    return cfg


def test_run_pipeline_default_omits_semantic_skipped(tmp_path):
    cfg = _write_semantic_only_config(tmp_path)
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n")

    # Empty diff -> can't-match filter drops the semantic rule.
    result = run_pipeline(str(cfg), str(target), "")
    assert "semantic_skipped" not in result, "default callers must not see new field"


def test_run_pipeline_include_skipped_surfaces_reason(tmp_path):
    cfg = _write_semantic_only_config(tmp_path)
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n")

    result = run_pipeline(str(cfg), str(target), "", include_skipped=True)
    assert "semantic_skipped" in result
    skipped = result["semantic_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["rule"] == "prose-rule"
    assert skipped[0]["reason"] == "empty-diff"


def test_run_pipeline_include_skipped_lists_evaluated_rules(tmp_path):
    cfg = _write_semantic_only_config(tmp_path)
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n")

    result = run_pipeline(str(cfg), str(target), "", include_skipped=True)
    assert "rules_evaluated" in result
    by_id = {r["rule"]: r for r in result["rules_evaluated"]}
    assert "prose-rule" in by_id
    assert by_id["prose-rule"]["verdict"] == "skipped"
    assert by_id["prose-rule"]["reason"] == "empty-diff"
    assert by_id["prose-rule"]["engine"] == "semantic"


def test_run_pipeline_include_skipped_records_passing_script(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "schema_version: 1\n"
        "rules:\n"
        "  banned-token:\n"
        '    description: "no banned-token in source"\n'
        "    engine: script\n"
        '    scope: ["*.py"]\n'
        "    severity: error\n"
        '    script: "grep -nE banned-token {file} && exit 1 || exit 0"\n'
    )
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n")  # no banned-token

    result = run_pipeline(str(cfg), str(target), "", include_skipped=True)
    by_id = {r["rule"]: r for r in result["rules_evaluated"]}
    assert by_id["banned-token"]["verdict"] == "pass"
    assert by_id["banned-token"]["engine"] == "script"


# ---- --explain CLI ----


def _run_cli(args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bully", *args],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(cwd) if cwd else None,
    )


def test_explain_flag_outputs_per_rule_verdict(tmp_path):
    cfg = _write_semantic_only_config(tmp_path)
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n")

    r = _run_cli(["lint", str(target), "--config", str(cfg), "--explain"])
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "prose-rule" in r.stdout
    assert "skipped" in r.stdout
    assert "empty-diff" in r.stdout


def test_explain_flag_shows_pass_verdict_for_clean_script_rule(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "schema_version: 1\n"
        "rules:\n"
        "  banned-token:\n"
        '    description: "no banned-token in source"\n'
        "    engine: script\n"
        '    scope: ["*.py"]\n'
        "    severity: error\n"
        '    script: "grep -nE banned-token {file} && exit 1 || exit 0"\n'
    )
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n")

    r = _run_cli(["lint", str(target), "--config", str(cfg), "--explain"])
    assert r.returncode == 0
    assert "banned-token" in r.stdout
    assert "pass" in r.stdout


# ---- hook-mode output unchanged (no regressions) ----


def test_hook_mode_output_does_not_include_semantic_skipped(tmp_path):
    _write_semantic_only_config(tmp_path)
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n")
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "",
            "new_string": "print('hi')\n",
        },
    }
    r = subprocess.run(
        [sys.executable, "-m", "bully", "--hook-mode"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(tmp_path),
    )
    # Hook-mode output is plain text on stderr (or empty); semantic_skipped
    # must not appear in either stream regardless of how the rule resolved.
    assert "semantic_skipped" not in r.stdout
    assert "semantic_skipped" not in r.stderr
