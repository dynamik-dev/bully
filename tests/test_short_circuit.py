"""Tests for SKIP_PATTERNS short-circuit (plan 4.6)."""

import pytest

from bully import SKIP_PATTERNS, _path_matches_skip, run_pipeline

RULE_YAML = (
    "rules:\n"
    "  always-fail:\n"
    '    description: "any file fails this"\n'
    "    engine: script\n"
    '    scope: "*"\n'
    "    severity: error\n"
    '    script: "exit 1"\n'
)


@pytest.mark.parametrize(
    "path",
    [
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "Cargo.lock",
        "app.min.js",
        "app.min.css",
        "style.min.anything",
        "dist/index.js",
        "build/output.bin",
        "__pycache__/foo.pyc",
        "module.generated.ts",
        "api.pb.go",
        "model.g.dart",
        "thing.freezed.dart",
    ],
)
def test_skip_patterns_match(path):
    assert _path_matches_skip(path), f"expected {path} to be skipped"


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "src/app.ts",
        "src/app.js",
        "README.md",
        "lib/foo.go",
    ],
)
def test_non_skip_patterns_do_not_match(path):
    assert not _path_matches_skip(path), f"did not expect {path} to be skipped"


def test_all_skip_patterns_are_strings():
    assert all(isinstance(p, str) for p in SKIP_PATTERNS)
    assert len(SKIP_PATTERNS) >= 10


def test_pipeline_short_circuits_on_lockfile(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "package-lock.json"
    target.write_text('{"dependencies": {}}')

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    # Despite a rule that would fail, we should skip the file entirely.
    assert result["status"] == "skipped"
    assert result["reason"] == "auto-generated"


def test_pipeline_short_circuits_on_dist_path(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    dist = tmp_path / "dist"
    dist.mkdir()
    target = dist / "bundle.js"
    target.write_text("var x = 1;\n")

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    assert result["status"] == "skipped"


def test_pipeline_does_not_skip_normal_source(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("print('hi')\n")

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    # Not skipped -> the script rule fires.
    assert result["status"] == "blocked"
