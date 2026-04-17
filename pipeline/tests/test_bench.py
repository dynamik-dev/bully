"""Bench harness tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline as pl


def test_phase_timer_default_is_noop_and_pipeline_still_runs(tmp_path, monkeypatch):
    """run_pipeline works unchanged when no phase_timer is passed."""
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  trivial:\n"
        "    description: trivial\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    target = tmp_path / "x.py"
    target.write_text("x = 1\n")
    result = pl.run_pipeline(str(cfg), str(target), diff="")
    assert result["status"] == "pass"


def test_phase_timer_records_each_phase(tmp_path, monkeypatch):
    """When a phase_timer is passed, it's called for each phase."""
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  trivial:\n"
        "    description: trivial\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    target = tmp_path / "x.py"
    target.write_text("x = 1\n")

    seen: list[str] = []

    class Recorder:
        def __call__(self, name):
            seen.append(name)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    pl.run_pipeline(str(cfg), str(target), diff="", phase_timer=Recorder())
    expected = {
        "skip_check",
        "trust_gate",
        "parse_config",
        "filter_rules",
        "script_exec",
        "ast_exec",
        "semantic_build",
    }
    assert set(seen) == expected


def test_bench_cli_entrypoint_exists():
    """`bully bench --help` exits cleanly (implies argparse wiring is in place)."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pipeline.pipeline", "bench", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "bench" in (result.stdout + result.stderr).lower()


def test_bench_module_imports():
    """bench module loads without requiring anthropic."""
    import bench

    assert hasattr(bench, "main")


def test_load_fixture_reads_config_and_metadata(tmp_path):
    """load_fixture returns a Fixture with config_path + metadata."""
    from bench import load_fixture

    fx_dir = tmp_path / "my-fixture"
    fx_dir.mkdir()
    (fx_dir / "config.yml").write_text(
        "rules:\n"
        "  r1:\n"
        "    description: d\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    (fx_dir / "fixture.json").write_text(
        '{"name": "my-fixture", "description": "x", '
        '"file_path": "src/a.py", "edit_type": "Edit", "diff": "--- a\\n"}'
    )
    fx = load_fixture(fx_dir)
    assert fx.name == "my-fixture"
    assert fx.file_path == "src/a.py"
    assert fx.edit_type == "Edit"
    assert fx.diff.startswith("--- a")
    assert fx.config_path.name == "config.yml"
    assert fx.config_path.exists()


def test_load_fixture_rejects_missing_files(tmp_path):
    """load_fixture raises when either expected file is missing."""
    import pytest
    from bench import FixtureError, load_fixture

    fx_dir = tmp_path / "bad"
    fx_dir.mkdir()
    # Missing both files.
    with pytest.raises(FixtureError, match="config.yml"):
        load_fixture(fx_dir)


def test_load_fixture_rejects_malformed_json(tmp_path):
    """load_fixture raises a clear error on malformed metadata JSON."""
    import pytest
    from bench import FixtureError, load_fixture

    fx_dir = tmp_path / "bad-json"
    fx_dir.mkdir()
    (fx_dir / "config.yml").write_text("rules: {}\n")
    (fx_dir / "fixture.json").write_text("{not json")
    with pytest.raises(FixtureError, match="fixture.json"):
        load_fixture(fx_dir)


def test_discover_fixtures_lists_all_subdirs(tmp_path):
    """discover_fixtures returns a sorted list of fixture directories."""
    from bench import discover_fixtures

    for name in ["zeta", "alpha", "mu"]:
        d = tmp_path / name
        d.mkdir()
        (d / "config.yml").write_text("rules: {}\n")
        (d / "fixture.json").write_text(
            '{"name": "' + name + '", "description": "", '
            '"file_path": "a.py", "edit_type": "Edit", "diff": ""}'
        )
    result = discover_fixtures(tmp_path)
    assert [f.name for f in result] == ["alpha", "mu", "zeta"]


def test_count_tokens_proxy_when_no_api_key(monkeypatch):
    """count_tokens falls back to char-count when no API key is present."""
    from bench import count_tokens

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = {"file": "x.py", "diff": "hello", "evaluate": []}
    count, method = count_tokens(payload, system="sys prompt")
    assert method == "proxy"
    import json as _json
    assert count == len(_json.dumps(payload)) + len("sys prompt")


def test_count_tokens_proxy_when_anthropic_missing(monkeypatch):
    """If `anthropic` is not importable, fall back to proxy even with key set."""
    import bench

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bench, "_import_anthropic", lambda: None)

    payload = {"x": 1}
    count, method = bench.count_tokens(payload, system="s")
    assert method == "proxy"


def test_count_tokens_api_path(monkeypatch):
    """With API key + anthropic client, call messages.count_tokens."""
    import bench

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class FakeResp:
        input_tokens = 42

    class FakeMessages:
        def count_tokens(self, **kwargs):
            assert kwargs["model"] == bench.BENCH_MODEL
            assert kwargs["system"] == "s"
            assert "content" in kwargs["messages"][0]
            return FakeResp()

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    class FakeAnthropic:
        Anthropic = FakeClient

    monkeypatch.setattr(bench, "_import_anthropic", lambda: FakeAnthropic)

    count, method = bench.count_tokens({"a": 1}, system="s")
    assert count == 42
    assert method == "count_tokens"


def test_load_evaluator_system_prompt_strips_frontmatter(tmp_path, monkeypatch):
    """System prompt is read from agents/bully-evaluator.md without frontmatter."""
    import bench

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "bully-evaluator.md").write_text(
        "---\n"
        "name: bully-evaluator\n"
        "model: sonnet\n"
        "---\n"
        "\n"
        "You are the evaluator. Apply each rule.\n"
    )
    monkeypatch.setattr(bench, "_repo_root", lambda: tmp_path)

    text = bench.load_evaluator_system_prompt()
    assert text.startswith("You are the evaluator")
    assert "---" not in text


def test_phasetimer_records_all_phase_durations():
    """PhaseTimer records a list of (name, ns) for each phase invocation."""
    import time

    from bench import PhaseTimer

    pt = PhaseTimer()
    with pt("a"):
        time.sleep(0.001)
    with pt("b"):
        time.sleep(0.002)
    results = pt.results_ns()
    assert "a" in results
    assert "b" in results
    # Each phase tracked at least one sample.
    assert len(results["a"]) == 1
    assert len(results["b"]) == 1
    assert results["a"][0] > 0
    assert results["b"][0] > 0


def test_run_fixture_returns_structured_result(tmp_path, monkeypatch):
    """run_fixture returns per-phase median/p95 plus cold-start and tokens."""
    from bench import Fixture, run_fixture

    fx_dir = tmp_path / "fx"
    fx_dir.mkdir()
    cfg = fx_dir / "config.yml"
    cfg.write_text(
        "rules:\n"
        "  r1:\n"
        "    description: d\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    (fx_dir / "fixture.json").write_text(
        '{"name": "fx", "description": "x", "file_path": "a.py",'
        ' "edit_type": "Edit", "diff": ""}'
    )
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")

    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    monkeypatch.chdir(tmp_path)

    fx = Fixture(
        name="fx",
        description="x",
        file_path=str(target),
        edit_type="Edit",
        diff="",
        config_path=cfg,
    )
    result = run_fixture(fx, iterations=3, use_api=False, skip_cold_start=True)
    assert result["name"] == "fx"
    assert "wall_ms_p50" in result
    assert "wall_ms_p95" in result
    assert "phases_ms" in result
    assert "skip_check" in result["phases_ms"]
    assert "parse_config" in result["phases_ms"]
    assert result["tokens"]["method"] == "n/a-no-semantic-rules"
    assert result["tokens"]["input"] == 0
