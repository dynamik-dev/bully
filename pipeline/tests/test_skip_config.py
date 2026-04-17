"""Tests for TASK-1.1: project-level `skip:` and ~/.bully-ignore.

The pipeline ships with a built-in `SKIP_PATTERNS` tuple covering common
auto-generated paths (lockfiles, minified bundles, dist/build dirs). This
test suite covers two ways to extend it without patching the source:

1. A top-level `skip:` key in `.bully.yml` (inline list or block list).
2. A user-global `~/.bully-ignore` file with one glob per line.

Both must merge with the built-ins; never replace them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import (  # noqa: E402
    SKIP_PATTERNS,
    ConfigError,
    _path_matches_skip,
    effective_skip_patterns,
    parse_config,
    run_pipeline,
)

RULE_BLOCK = (
    "rules:\n"
    "  always-fail:\n"
    '    description: "any file fails this"\n'
    "    engine: script\n"
    '    scope: "*"\n'
    "    severity: error\n"
    '    script: "exit 1"\n'
)


# ---- parser ----


def test_parse_skip_inline_list(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text('skip: ["_build/**", "vendor/**"]\n' + RULE_BLOCK)
    parse_config(str(cfg))  # must not raise
    patterns = effective_skip_patterns(str(cfg), include_user_global=False)
    assert "_build/**" in patterns
    assert "vendor/**" in patterns


def test_parse_skip_block_list(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text("skip:\n  - _build/**\n  - target/**\n" + RULE_BLOCK)
    parse_config(str(cfg))  # must not raise
    patterns = effective_skip_patterns(str(cfg), include_user_global=False)
    assert "_build/**" in patterns
    assert "target/**" in patterns


def test_skip_inherits_built_in_defaults(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text('skip: ["custom/**"]\n' + RULE_BLOCK)
    patterns = effective_skip_patterns(str(cfg), include_user_global=False)
    # Project skip merged with the built-ins, not replaced.
    for built_in in SKIP_PATTERNS:
        assert built_in in patterns
    assert "custom/**" in patterns


def test_skip_malformed_value_raises(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text("skip: not-a-list\n" + RULE_BLOCK)
    try:
        parse_config(str(cfg))
    except ConfigError as e:
        assert "skip" in str(e).lower()
        assert "list" in str(e).lower()
    else:
        raise AssertionError("expected ConfigError for non-list skip value")


# ---- user-global ~/.bully-ignore ----


def test_user_global_skips_loaded_when_present(tmp_path, monkeypatch):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(RULE_BLOCK)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".bully-ignore").write_text("# this is a comment\n\nnode_modules/**\ntarget/**\n")
    monkeypatch.setenv("HOME", str(fake_home))

    patterns = effective_skip_patterns(str(cfg))
    assert "node_modules/**" in patterns
    assert "target/**" in patterns


def test_user_global_skips_silent_when_absent(tmp_path, monkeypatch):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(RULE_BLOCK)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    patterns = effective_skip_patterns(str(cfg))
    # Built-ins still present; nothing extra added.
    for built_in in SKIP_PATTERNS:
        assert built_in in patterns


# ---- no-config-yet behavior preserved ----


def test_existing_configs_without_skip_unchanged(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(RULE_BLOCK)  # no skip: key
    patterns = effective_skip_patterns(str(cfg), include_user_global=False)
    assert tuple(patterns) == SKIP_PATTERNS


# ---- _path_matches_skip with extra patterns ----


def test_path_matches_skip_extra_patterns():
    assert not _path_matches_skip("_build/x.html")  # not in built-ins
    assert _path_matches_skip("_build/x.html", extra_patterns=("_build/**",))


# ---- end-to-end: project skip suppresses rule firing ----


def test_run_pipeline_skips_via_project_skip(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text('skip: ["_build/**"]\n' + RULE_BLOCK)
    target = tmp_path / "_build" / "out.html"
    target.parent.mkdir()
    target.write_text("<html></html>\n")

    result = run_pipeline(str(cfg), str(target), "")
    assert result["status"] == "skipped"
    assert result["reason"] == "auto-generated"


def test_run_pipeline_skips_via_user_global(tmp_path, monkeypatch):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(RULE_BLOCK)
    target = tmp_path / "node_modules" / "x.js"
    target.parent.mkdir()
    target.write_text("// vendor code\n")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".bully-ignore").write_text("node_modules/**\n")
    monkeypatch.setenv("HOME", str(fake_home))

    result = run_pipeline(str(cfg), str(target), "")
    assert result["status"] == "skipped"
