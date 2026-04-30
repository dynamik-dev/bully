"""Tests for the ast engine: parser, language inference, JSON adapter,
executor, and graceful skip when ast-grep is missing."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bully import (
    ConfigError,
    Rule,
    _infer_ast_language,
    _parse_ast_grep_json,
    execute_ast_rule,
    parse_config,
    run_pipeline,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---- parser tests -------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".bully.yml"
    p.write_text(body)
    return p


def test_parse_ast_rule_with_pattern(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  no-any-cast:\n"
        "    description: No `as any`\n"
        "    engine: ast\n"
        '    scope: ["*.ts", "*.tsx"]\n'
        "    severity: error\n"
        '    pattern: "$EXPR as any"\n',
    )
    rules = parse_config(str(cfg))
    assert len(rules) == 1
    r = rules[0]
    assert r.id == "no-any-cast"
    assert r.engine == "ast"
    assert r.pattern == "$EXPR as any"
    assert r.language is None  # inferred at runtime
    assert r.script is None


def test_parse_ast_rule_with_explicit_language(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  no-var-dump:\n"
        "    description: No var_dump\n"
        "    engine: ast\n"
        '    scope: "*.php"\n'
        "    severity: error\n"
        '    pattern: "var_dump($$$)"\n'
        "    language: php\n",
    )
    rules = parse_config(str(cfg))
    assert rules[0].language == "php"
    assert rules[0].pattern == "var_dump($$$)"


def test_parse_ast_rule_without_pattern_raises(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  bad:\n"
        "    description: missing pattern\n"
        "    engine: ast\n"
        '    scope: "*.ts"\n'
        "    severity: error\n",
    )
    with pytest.raises(ConfigError) as exc:
        parse_config(str(cfg))
    assert "pattern" in str(exc.value)


def test_parse_ast_rule_with_script_is_contradiction(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  bad:\n"
        "    description: contradiction\n"
        "    engine: ast\n"
        '    scope: "*.ts"\n'
        "    severity: error\n"
        '    pattern: "foo"\n'
        '    script: "grep foo {file}"\n',
    )
    with pytest.raises(ConfigError) as exc:
        parse_config(str(cfg))
    assert "script" in str(exc.value).lower()


def test_pattern_on_script_engine_raises(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  bad:\n"
        "    description: wrong engine for pattern\n"
        "    engine: script\n"
        '    scope: "*.ts"\n'
        "    severity: error\n"
        '    script: "true"\n'
        '    pattern: "foo"\n',
    )
    with pytest.raises(ConfigError) as exc:
        parse_config(str(cfg))
    assert "pattern" in str(exc.value).lower()


def test_language_on_semantic_engine_raises(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  bad:\n"
        "    description: wrong engine for language\n"
        "    engine: semantic\n"
        '    scope: "*.ts"\n'
        "    severity: error\n"
        "    language: ts\n",
    )
    with pytest.raises(ConfigError) as exc:
        parse_config(str(cfg))
    assert "language" in str(exc.value).lower()


def test_unknown_engine_mentions_ast(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  bad:\n"
        "    description: x\n"
        "    engine: banana\n"
        '    scope: "*.ts"\n'
        "    severity: error\n",
    )
    with pytest.raises(ConfigError) as exc:
        parse_config(str(cfg))
    assert "ast" in str(exc.value)


# ---- language inference -------------------------------------------------


def test_infer_ts_from_tsx():
    assert _infer_ast_language("src/App.tsx") == "tsx"


def test_infer_csharp_from_cs():
    assert _infer_ast_language("Services/User.cs") == "csharp"


def test_infer_php_from_php():
    assert _infer_ast_language("app/Models/User.php") == "php"


def test_infer_unknown_extension_returns_none():
    assert _infer_ast_language("notes.txt") is None


def test_infer_is_case_insensitive():
    assert _infer_ast_language("App.TSX") == "tsx"


# ---- JSON adapter -------------------------------------------------------


def test_parse_ast_grep_json_empty_array():
    assert _parse_ast_grep_json("rule", "error", "[]") == []


def test_parse_ast_grep_json_single_match():
    payload = json.dumps(
        [
            {
                "range": {"start": {"line": 41, "column": 0}, "end": {"line": 41, "column": 20}},
                "lines": "    const x = foo as any;",
            }
        ]
    )
    violations = _parse_ast_grep_json("no-any-cast", "error", payload)
    assert len(violations) == 1
    v = violations[0]
    assert v.rule == "no-any-cast"
    assert v.engine == "ast"
    assert v.severity == "error"
    assert v.line == 42  # 0-indexed -> 1-indexed
    assert "foo as any" in v.description


def test_parse_ast_grep_json_multiple_matches():
    payload = json.dumps(
        [
            {"range": {"start": {"line": 0}}, "lines": "first match"},
            {"range": {"start": {"line": 10}}, "lines": "second match"},
        ]
    )
    violations = _parse_ast_grep_json("r", "warning", payload)
    assert [v.line for v in violations] == [1, 11]
    assert all(v.engine == "ast" for v in violations)


def test_parse_ast_grep_json_malformed_returns_empty():
    assert _parse_ast_grep_json("r", "error", "not json at all") == []


def test_parse_ast_grep_json_non_array_returns_empty():
    assert _parse_ast_grep_json("r", "error", '{"oops": true}') == []


# ---- executor tests (mock subprocess) ----------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_execute_ast_rule_no_match_returns_empty(tmp_path: Path):
    target = tmp_path / "file.ts"
    target.write_text("const x = 1;\n")
    rule = Rule(
        id="no-any-cast",
        description="no as any",
        engine="ast",
        scope=("*.ts",),
        severity="error",
        pattern="$E as any",
    )
    with patch("bully.engines.ast_grep.subprocess.run") as mock_run:
        mock_run.return_value = _FakeCompleted("[]", returncode=0)
        violations = execute_ast_rule(rule, str(target))
    assert violations == []
    called_cmd = mock_run.call_args.args[0]
    assert called_cmd[0] == "ast-grep"
    assert "--pattern" in called_cmd
    assert "$E as any" in called_cmd
    assert "--lang" in called_cmd
    assert "ts" in called_cmd
    # shell=False for ast rules (unlike script rules).
    assert mock_run.call_args.kwargs.get("shell") is False


def test_execute_ast_rule_match_produces_violation(tmp_path: Path):
    target = tmp_path / "file.ts"
    target.write_text("x;\n")
    rule = Rule(
        id="no-any-cast",
        description="no as any",
        engine="ast",
        scope=("*.ts",),
        severity="error",
        pattern="$E as any",
    )
    fake_out = json.dumps([{"range": {"start": {"line": 0}}, "lines": "const x = y as any;"}])
    with patch("bully.engines.ast_grep.subprocess.run") as mock_run:
        mock_run.return_value = _FakeCompleted(fake_out, returncode=1)
        violations = execute_ast_rule(rule, str(target))
    assert len(violations) == 1
    assert violations[0].rule == "no-any-cast"
    assert violations[0].engine == "ast"
    assert violations[0].line == 1


def test_execute_ast_rule_unknown_language_returns_config_violation(tmp_path: Path):
    target = tmp_path / "weird.xyz"
    target.write_text("data\n")
    rule = Rule(
        id="r",
        description="desc",
        engine="ast",
        scope=("*.xyz",),
        severity="error",
        pattern="foo",
    )
    violations = execute_ast_rule(rule, str(target))
    assert len(violations) == 1
    assert "could not infer" in violations[0].description


def test_execute_ast_rule_explicit_language_overrides_inference(tmp_path: Path):
    target = tmp_path / "file.ts"
    target.write_text("x;\n")
    rule = Rule(
        id="r",
        description="desc",
        engine="ast",
        scope=("*.ts",),
        severity="error",
        pattern="foo",
        language="tsx",
    )
    with patch("bully.engines.ast_grep.subprocess.run") as mock_run:
        mock_run.return_value = _FakeCompleted("[]", returncode=0)
        execute_ast_rule(rule, str(target))
    called_cmd = mock_run.call_args.args[0]
    assert "tsx" in called_cmd
    assert "ts" not in [c for c in called_cmd if c == "ts"]


def test_execute_ast_rule_tool_missing_returns_empty(tmp_path: Path):
    target = tmp_path / "file.ts"
    target.write_text("x;\n")
    rule = Rule(
        id="r",
        description="desc",
        engine="ast",
        scope=("*.ts",),
        severity="error",
        pattern="$E as any",
    )
    with patch("bully.engines.ast_grep.subprocess.run", side_effect=FileNotFoundError):
        assert execute_ast_rule(rule, str(target)) == []


def test_execute_ast_rule_error_exit_surfaces_stderr(tmp_path: Path):
    target = tmp_path / "file.ts"
    target.write_text("x;\n")
    rule = Rule(
        id="r",
        description="desc",
        engine="ast",
        scope=("*.ts",),
        severity="error",
        pattern="broken(pattern",
    )
    with patch("bully.engines.ast_grep.subprocess.run") as mock_run:
        mock_run.return_value = _FakeCompleted("", stderr="error: invalid pattern\n", returncode=2)
        violations = execute_ast_rule(rule, str(target))
    assert len(violations) == 1
    assert "ast-grep failed" in violations[0].description
    assert "invalid pattern" in violations[0].description


# ---- pipeline integration: graceful skip when tool missing --------------


def test_pipeline_skips_ast_rules_when_tool_missing(tmp_path: Path, capsys):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  no-any:\n"
        "    description: demo\n"
        "    engine: ast\n"
        '    scope: "*.ts"\n'
        "    severity: error\n"
        '    pattern: "$E as any"\n',
    )
    target = tmp_path / "file.ts"
    target.write_text("const x = 1;\n")

    with patch("bully.runtime.runner.ast_grep_available", return_value=False):
        result = run_pipeline(str(cfg), str(target), "")

    assert result["status"] == "pass"
    captured = capsys.readouterr()
    assert "ast-grep not on PATH" in captured.err
    assert "brew install ast-grep" in captured.err


def test_pipeline_runs_ast_rules_when_tool_present(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  no-any:\n"
        "    description: demo\n"
        "    engine: ast\n"
        '    scope: "*.ts"\n'
        "    severity: error\n"
        '    pattern: "$E as any"\n',
    )
    target = tmp_path / "file.ts"
    target.write_text("const x = foo as any;\n")

    fake_out = json.dumps([{"range": {"start": {"line": 0}}, "lines": "const x = foo as any;"}])

    with (
        patch("bully.runtime.runner.ast_grep_available", return_value=True),
        patch("bully.engines.ast_grep.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _FakeCompleted(fake_out, returncode=1)
        result = run_pipeline(str(cfg), str(target), "")

    assert result["status"] == "blocked"
    assert result["violations"][0]["rule"] == "no-any"
    assert result["violations"][0]["engine"] == "ast"


def test_pipeline_ast_warning_does_not_block(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        "schema_version: 1\n"
        "rules:\n"
        "  note-pattern:\n"
        "    description: demo\n"
        "    engine: ast\n"
        '    scope: "*.ts"\n'
        "    severity: warning\n"
        '    pattern: "$E as any"\n',
    )
    target = tmp_path / "file.ts"
    target.write_text("const x = foo as any;\n")

    fake_out = json.dumps([{"range": {"start": {"line": 0}}, "lines": "const x = foo as any;"}])

    with (
        patch("bully.runtime.runner.ast_grep_available", return_value=True),
        patch("bully.engines.ast_grep.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _FakeCompleted(fake_out, returncode=1)
        result = run_pipeline(str(cfg), str(target), "")

    assert result["status"] == "pass"
    assert "warnings" in result and result["warnings"][0]["rule"] == "note-pattern"
