"""Bench harness tests."""

from __future__ import annotations

import sys
from pathlib import Path

import bully as pl


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
        [sys.executable, "-m", "bully", "bench", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "bench" in (result.stdout + result.stderr).lower()


def test_bench_module_imports():
    """bench module loads without requiring anthropic."""
    import bully.bench as bench

    assert hasattr(bench, "main")


def test_load_fixture_reads_config_and_metadata(tmp_path):
    """load_fixture returns a Fixture with config_path + metadata."""
    from bully.bench import load_fixture

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

    from bully.bench import FixtureError, load_fixture

    fx_dir = tmp_path / "bad"
    fx_dir.mkdir()
    # Missing both files.
    with pytest.raises(FixtureError, match="config.yml"):
        load_fixture(fx_dir)


def test_load_fixture_rejects_malformed_json(tmp_path):
    """load_fixture raises a clear error on malformed metadata JSON."""
    import pytest

    from bully.bench import FixtureError, load_fixture

    fx_dir = tmp_path / "bad-json"
    fx_dir.mkdir()
    (fx_dir / "config.yml").write_text("rules: {}\n")
    (fx_dir / "fixture.json").write_text("{not json")
    with pytest.raises(FixtureError, match="fixture.json"):
        load_fixture(fx_dir)


def test_discover_fixtures_lists_all_subdirs(tmp_path):
    """discover_fixtures returns a sorted list of fixture directories."""
    from bully.bench import discover_fixtures

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
    from bully.bench import count_tokens

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = {"file": "x.py", "diff": "hello", "evaluate": []}
    count, method = count_tokens(payload, system="sys prompt")
    assert method == "proxy"
    import json as _json

    assert count == len(_json.dumps(payload)) + len("sys prompt")


def test_count_tokens_proxy_when_anthropic_missing(monkeypatch):
    """If `anthropic` is not importable, fall back to proxy even with key set."""
    import bully.bench as bench

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr("bully.bench.dispatch.import_anthropic", lambda: None)

    payload = {"x": 1}
    count, method = bench.count_tokens(payload, system="s")
    assert method == "proxy"


def test_count_tokens_api_path(monkeypatch):
    """With API key + anthropic client, call messages.count_tokens."""
    import bully.bench as bench

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

    monkeypatch.setattr("bully.bench.dispatch.import_anthropic", lambda: FakeAnthropic)

    count, method = bench.count_tokens({"a": 1}, system="s")
    assert count == 42
    assert method == "count_tokens"


def test_load_evaluator_system_prompt_strips_frontmatter(tmp_path, monkeypatch):
    """System prompt is read from agents/bully-evaluator.md without frontmatter."""
    import bully.bench as bench

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
    monkeypatch.setattr("bully.bench.dispatch.repo_root", lambda: tmp_path)

    text = bench.load_evaluator_system_prompt()
    assert text.startswith("You are the evaluator")
    assert "---" not in text


def test_phasetimer_records_all_phase_durations():
    """PhaseTimer records a list of (name, ns) for each phase invocation."""
    import time

    from bully.bench import PhaseTimer

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
    from bully.bench import Fixture, run_fixture

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
        '{"name": "fx", "description": "x", "file_path": "a.py", "edit_type": "Edit", "diff": ""}'
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


def test_run_mode_a_writes_history_line(tmp_path, monkeypatch):
    """Mode A writes one JSONL line per run with fixture results + aggregates."""
    import json as _json

    from bully.bench import run_mode_a

    # Build two fixtures.
    fixtures_root = tmp_path / "fixtures"
    for name in ("a", "b"):
        d = fixtures_root / name
        d.mkdir(parents=True)
        (d / "config.yml").write_text(
            "rules:\n"
            "  r:\n"
            "    description: d\n"
            "    engine: script\n"
            "    scope: '**/*.py'\n"
            "    severity: warning\n"
            "    script: 'exit 0'\n"
        )
        (d / "fixture.json").write_text(
            '{"name": "' + name + '", "description": "x",'
            ' "file_path": "x.py", "edit_type": "Edit", "diff": ""}'
        )
    (tmp_path / "x.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")

    history_path = tmp_path / "history.jsonl"
    rc = run_mode_a(
        fixtures_dir=fixtures_root,
        history_path=history_path,
        use_api=False,
        iterations=2,
        skip_cold_start=True,
        emit_json=True,
    )
    assert rc == 0
    assert history_path.is_file()
    lines = history_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = _json.loads(lines[0])
    assert "ts" in record
    assert "fixtures" in record
    assert len(record["fixtures"]) == 2
    assert {f["name"] for f in record["fixtures"]} == {"a", "b"}
    assert "aggregates" in record
    assert "total_wall_ms_p50" in record["aggregates"]


def test_run_mode_a_errors_when_no_fixtures(tmp_path, capsys):
    """Mode A returns non-zero and prints an error when no fixtures exist."""
    from bully.bench import run_mode_a

    history_path = tmp_path / "h.jsonl"
    rc = run_mode_a(
        fixtures_dir=tmp_path / "missing",
        history_path=history_path,
        use_api=False,
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "no fixtures" in err.lower()


def test_compare_reports_deltas_between_last_two_runs(tmp_path, capsys):
    """--compare prints a delta table for the last two history entries."""
    import json as _json

    from bully.bench import run_compare

    history = tmp_path / "h.jsonl"
    older = {
        "ts": "2026-04-15T10:00:00Z",
        "git_sha": "aaa",
        "fixtures": [
            {"name": "a", "wall_ms_p50": 10.0, "tokens": {"input": 100, "method": "count_tokens"}},
        ],
        "aggregates": {
            "total_wall_ms_p50": 10.0,
            "total_input_tokens": 100,
        },
    }
    newer = {
        "ts": "2026-04-17T12:00:00Z",
        "git_sha": "bbb",
        "fixtures": [
            {"name": "a", "wall_ms_p50": 15.0, "tokens": {"input": 120, "method": "count_tokens"}},
        ],
        "aggregates": {
            "total_wall_ms_p50": 15.0,
            "total_input_tokens": 120,
        },
    }
    history.write_text(_json.dumps(older) + "\n" + _json.dumps(newer) + "\n")

    rc = run_compare(history_path=history)
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaa" in out and "bbb" in out
    # Wall delta is +5.00; token delta is +20
    assert "+5" in out
    assert "+20" in out


def test_compare_fails_when_fewer_than_two_runs(tmp_path, capsys):
    """--compare needs at least two runs to produce a delta."""
    import json as _json

    from bully.bench import run_compare

    history = tmp_path / "h.jsonl"
    history.write_text(_json.dumps({"ts": "t", "fixtures": [], "aggregates": {}}) + "\n")

    rc = run_compare(history_path=history)
    assert rc != 0
    err = capsys.readouterr().err
    assert "two runs" in err.lower() or "2 runs" in err.lower()


def test_mode_b_reports_floor_and_per_rule(tmp_path, monkeypatch):
    """Mode B computes floor, per-rule marginal, and diff scaling."""
    from bully.bench import run_mode_b

    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  sem-long:\n"
        "    description: 'A somewhat long description that should cost more tokens than a short one'\n"
        "    engine: semantic\n"
        "    scope: '**/*.py'\n"
        "    severity: error\n"
        "  sem-short:\n"
        "    description: short\n"
        "    engine: semantic\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "  script-only:\n"
        "    description: scripted check\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: error\n"
        "    script: 'exit 0'\n"
    )
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")

    result = run_mode_b(config_path=cfg, use_api=False, emit_json=True)
    assert result is not None
    report = result
    assert report["floor_tokens"] > 0
    assert len(report["per_rule"]) == 2  # two semantic rules
    long_cost = next(r["tokens"] for r in report["per_rule"] if r["id"] == "sem-long")
    short_cost = next(r["tokens"] for r in report["per_rule"] if r["id"] == "sem-short")
    assert long_cost > short_cost
    assert "diff_scaling" in report
    sizes = [row["added_lines"] for row in report["diff_scaling"]]
    assert sizes == [1, 10, 100, 1000]
    # Monotonically non-decreasing as diff grows.
    totals = [row["total_tokens"] for row in report["diff_scaling"]]
    assert totals == sorted(totals)
    # Script rule listed separately as zero-cost model-wise.
    assert any(r["id"] == "script-only" for r in report["deterministic_rules"])


def test_mode_b_handles_config_with_no_semantic_rules(tmp_path, monkeypatch):
    """Empty-semantic config reports floor=0 and empty per-rule."""
    from bully.bench import run_mode_b

    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        "rules:\n"
        "  scripted:\n"
        "    description: d\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")

    result = run_mode_b(config_path=cfg, use_api=False, emit_json=True)
    assert result is not None
    report = result
    assert report["floor_tokens"] == 0
    assert report["per_rule"] == []
    assert len(report["deterministic_rules"]) == 1


def test_mode_b_errors_when_config_missing(tmp_path, capsys):
    """Missing config path yields a clear error."""
    from bully.bench import run_mode_b

    result = run_mode_b(config_path=tmp_path / "nope.yml", use_api=False)
    assert result is None
    err = capsys.readouterr().err
    assert "not found" in err.lower()


def test_run_fixture_restores_trust_env_var(tmp_path, monkeypatch):
    """run_fixture must not leak BULLY_TRUST_ALL to the caller's env."""
    from bully.bench import Fixture, run_fixture

    fx_dir = tmp_path / "fx"
    fx_dir.mkdir()
    cfg = fx_dir / "config.yml"
    cfg.write_text(
        "rules:\n"
        "  r:\n"
        "    description: d\n"
        "    engine: script\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
        "    script: 'exit 0'\n"
    )
    (fx_dir / "fixture.json").write_text(
        '{"name": "fx", "description": "x", "file_path": "a.py", "edit_type": "Edit", "diff": ""}'
    )
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BULLY_TRUST_ALL", raising=False)

    fx = Fixture(
        name="fx",
        description="x",
        file_path=str(target),
        edit_type="Edit",
        diff="",
        config_path=cfg,
    )
    run_fixture(fx, iterations=1, use_api=False, skip_cold_start=True)
    # Env var was absent before the call; it must be absent after.
    import os as _os

    assert "BULLY_TRUST_ALL" not in _os.environ


def test_bench_config_and_compare_are_mutually_exclusive():
    """--config and --compare can't be used together."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "bully", "bench", "--config", "foo.yml", "--compare"],
        capture_output=True,
        text=True,
    )
    # argparse exits 2 on mutually-exclusive conflict.
    assert result.returncode == 2
    assert "not allowed" in result.stderr.lower() or "mutually exclusive" in result.stderr.lower()


def test_real_fixtures_complete_successfully(tmp_path, monkeypatch):
    """All authored fixtures under bench/fixtures/ run without errors."""
    import json as _json

    from bully.bench import run_mode_a

    repo_root = Path(__file__).resolve().parent.parent.parent
    fixtures_dir = repo_root / "bench" / "fixtures"
    if not fixtures_dir.is_dir():
        import pytest

        pytest.skip("fixtures directory not present")

    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    monkeypatch.chdir(repo_root)
    history = tmp_path / "history.jsonl"

    rc = run_mode_a(
        fixtures_dir=fixtures_dir,
        history_path=history,
        use_api=False,
        iterations=2,
        skip_cold_start=True,
    )
    assert rc == 0
    record = _json.loads(history.read_text().strip().splitlines()[-1])
    names = {f["name"] for f in record["fixtures"]}
    assert len(names) >= 8
    # Auto-generated skip fixture should short-circuit (wall time near zero).
    skip_fx = next(f for f in record["fixtures"] if "auto-generated-skip" in f["name"])
    assert skip_fx["wall_ms_p50"] < 10.0


def test_full_dispatch_real_api_path(monkeypatch):
    """full_dispatch calls messages.create and returns (input, output, 'full')."""
    import bully.bench as bench

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class FakeUsage:
        input_tokens = 1200
        output_tokens = 85

    class FakeResp:
        usage = FakeUsage()

    class FakeMessages:
        def create(self, **kwargs):
            assert kwargs["model"] == bench.BENCH_MODEL
            assert kwargs["max_tokens"] == 1024
            assert kwargs["system"] == "s"
            return FakeResp()

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    class FakeAnthropic:
        Anthropic = FakeClient

    monkeypatch.setattr("bully.bench.dispatch.import_anthropic", lambda: FakeAnthropic)

    input_tok, output_tok, method = bench.full_dispatch({"a": 1}, system="s")
    assert input_tok == 1200
    assert output_tok == 85
    assert method == "full"


def test_full_dispatch_falls_back_to_count_tokens_on_failure(monkeypatch):
    """Any exception from messages.create falls back to (count_tokens, 0, method)."""
    import bully.bench as bench

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr("bully.bench.dispatch.import_anthropic", lambda: None)

    input_tok, output_tok, method = bench.full_dispatch({"a": 1}, system="s")
    assert output_tok == 0
    assert method == "proxy"
    # Proxy = len(json.dumps(payload)) + len(system)
    import json as _json

    assert input_tok == len(_json.dumps({"a": 1})) + len("s")


def test_estimate_cost_usd_matches_pricing_constants():
    """_estimate_cost_usd uses the published per-million rates."""
    import bully.bench as bench

    # 1M input + 1M output should sum the two constants.
    cost = bench._estimate_cost_usd(1_000_000, 1_000_000)
    assert cost == bench.BENCH_INPUT_PRICE_PER_MTOK + bench.BENCH_OUTPUT_PRICE_PER_MTOK


def test_run_fixture_full_mode_captures_output_and_cost(tmp_path, monkeypatch):
    """run_fixture(full=True) records input + output + cost_usd."""
    from bully.bench import Fixture, run_fixture

    fx_dir = tmp_path / "fx"
    fx_dir.mkdir()
    cfg = fx_dir / "config.yml"
    cfg.write_text(
        "rules:\n"
        "  sem:\n"
        "    description: a semantic rule\n"
        "    engine: semantic\n"
        "    scope: '**/*.py'\n"
        "    severity: warning\n"
    )
    (fx_dir / "fixture.json").write_text(
        '{"name": "fx", "description": "x", "file_path": "a.py", "edit_type": "Edit", "diff": ""}'
    )
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    # Stub full_dispatch so the test doesn't hit the real API.
    monkeypatch.setattr(
        "bully.bench.modes.single.full_dispatch",
        lambda payload, *, system, max_tokens=1024: (1500, 100, "full"),
    )

    fx = Fixture(
        name="fx",
        description="x",
        file_path=str(target),
        edit_type="Edit",
        diff="",
        config_path=cfg,
    )
    result = run_fixture(fx, iterations=2, skip_cold_start=True, full=True)
    assert result["tokens"]["method"] == "full"
    assert result["tokens"]["input"] == 1500
    assert result["tokens"]["output"] == 100
    # cost = 1500 * 3/M + 100 * 15/M
    expected = 1500 * 3.0 / 1_000_000 + 100 * 15.0 / 1_000_000
    assert abs(result["tokens"]["cost_usd"] - expected) < 1e-12


def test_run_mode_a_full_mode_aggregates_output_and_cost(tmp_path, monkeypatch):
    """run_mode_a(full=True) aggregates output tokens + cost_usd."""
    import json as _json

    from bully.bench import run_mode_a

    fixtures_root = tmp_path / "fixtures"
    for name in ("a", "b"):
        d = fixtures_root / name
        d.mkdir(parents=True)
        (d / "config.yml").write_text(
            "rules:\n"
            "  sem:\n"
            "    description: d\n"
            "    engine: semantic\n"
            "    scope: '*.py'\n"
            "    severity: warning\n"
        )
        (d / "fixture.json").write_text(
            '{"name": "' + name + '", "description": "x",'
            ' "file_path": "x.py", "edit_type": "Edit", "diff": ""}'
        )
    (tmp_path / "x.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        "bully.bench.modes.single.full_dispatch",
        lambda payload, *, system, max_tokens=1024: (500, 50, "full"),
    )

    history_path = tmp_path / "history.jsonl"
    rc = run_mode_a(
        fixtures_dir=fixtures_root,
        history_path=history_path,
        iterations=2,
        skip_cold_start=True,
        full=True,
        emit_json=True,
    )
    assert rc == 0
    record = _json.loads(history_path.read_text().strip().splitlines()[-1])
    agg = record["aggregates"]
    assert agg["total_input_tokens"] == 1000
    assert agg["total_output_tokens"] == 100
    assert agg["tokens_method"] == "full"
    # 1000 * 3/M + 100 * 15/M
    expected = 1000 * 3.0 / 1_000_000 + 100 * 15.0 / 1_000_000
    assert abs(agg["total_cost_usd"] - expected) < 1e-12
