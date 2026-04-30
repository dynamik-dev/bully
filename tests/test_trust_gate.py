"""Tests for the .bully.yml trust boundary gate.

These tests override the global conftest.py behavior that sets
BULLY_TRUST_ALL=1, because the whole point is to exercise the gate itself.
"""

import json
from pathlib import Path

import pytest

from bully import (
    _cmd_trust,
    _config_checksum,
    _trust_status,
    run_pipeline,
)


def _write_config(tmp_path: Path, body: str = None) -> Path:
    p = tmp_path / ".bully.yml"
    p.write_text(
        body
        or (
            "schema_version: 1\n"
            "rules:\n"
            "  tiny:\n"
            "    description: match nothing\n"
            "    engine: script\n"
            '    scope: "*.txt"\n'
            "    severity: warning\n"
            '    script: "true"\n'
        )
    )
    return p


@pytest.fixture
def isolated_trust_store(tmp_path, monkeypatch):
    """Redirect the trust store to a test-local file and drop BULLY_TRUST_ALL."""
    store = tmp_path / "bully-trust.json"
    monkeypatch.setenv("BULLY_TRUST_STORE", str(store))
    monkeypatch.delenv("BULLY_TRUST_ALL", raising=False)
    return store


# ---- _trust_status ------------------------------------------------------


def test_status_untrusted_when_never_allowed(tmp_path, isolated_trust_store):
    cfg = _write_config(tmp_path)
    status, detail = _trust_status(str(cfg))
    assert status == "untrusted"
    assert "never" in detail


def test_status_trusted_after_cmd_trust(tmp_path, isolated_trust_store, capsys):
    cfg = _write_config(tmp_path)
    _cmd_trust(str(cfg), refresh=False)
    status, _ = _trust_status(str(cfg))
    assert status == "trusted"


def test_status_mismatch_when_config_changes(tmp_path, isolated_trust_store):
    cfg = _write_config(tmp_path)
    _cmd_trust(str(cfg), refresh=False)
    cfg.write_text(cfg.read_text() + "# mutated\n")
    status, detail = _trust_status(str(cfg))
    assert status == "mismatch"
    assert "checksum changed" in detail


def test_status_trusted_via_env_override(tmp_path, monkeypatch, isolated_trust_store):
    cfg = _write_config(tmp_path)
    # Even without a store entry, the env override grants trust.
    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    status, detail = _trust_status(str(cfg))
    assert status == "trusted"
    assert "BULLY_TRUST_ALL" in detail


def test_env_override_value_must_be_one(tmp_path, monkeypatch, isolated_trust_store):
    cfg = _write_config(tmp_path)
    # Any value other than the literal "1" is not a bypass.
    monkeypatch.setenv("BULLY_TRUST_ALL", "true")
    status, _ = _trust_status(str(cfg))
    assert status == "untrusted"


# ---- checksum ------------------------------------------------------------


def test_checksum_changes_when_contents_change(tmp_path):
    cfg = _write_config(tmp_path)
    first = _config_checksum(str(cfg))
    cfg.write_text(cfg.read_text() + "# mutated\n")
    second = _config_checksum(str(cfg))
    assert first != second
    assert len(first) == 64  # SHA256 hex


def test_checksum_returns_empty_for_missing_file(tmp_path):
    assert _config_checksum(str(tmp_path / "does-not-exist.yml")) == ""


def test_checksum_includes_extends_targets(tmp_path):
    base = tmp_path / "base.yml"
    base.write_text(
        "schema_version: 1\n"
        "rules:\n"
        "  base-rule:\n"
        "    description: base\n"
        "    engine: script\n"
        '    scope: "*.txt"\n'
        "    severity: warning\n"
        '    script: "true"\n'
    )
    child = tmp_path / ".bully.yml"
    child.write_text(f'schema_version: 1\nextends: ["{base.as_posix()}"]\nrules: {{}}\n')
    before = _config_checksum(str(child))
    # Mutate the base: the child's own bytes are unchanged, but the
    # composite checksum must drift because the extends target changed.
    base.write_text(base.read_text() + "# tweak\n")
    after = _config_checksum(str(child))
    assert before != after


# ---- _cmd_trust ---------------------------------------------------------


def test_cmd_trust_writes_store_entry(tmp_path, isolated_trust_store, capsys):
    cfg = _write_config(tmp_path)
    rc = _cmd_trust(str(cfg), refresh=False)
    assert rc == 0
    data = json.loads(isolated_trust_store.read_text())
    entry = data["allowed"][str(cfg.resolve())]
    assert len(entry["checksum"]) == 64
    assert entry["allowed_at"].endswith("Z")
    captured = capsys.readouterr()
    assert "trusted" in captured.out


def test_cmd_trust_idempotent_without_refresh(tmp_path, isolated_trust_store, capsys):
    cfg = _write_config(tmp_path)
    _cmd_trust(str(cfg), refresh=False)
    capsys.readouterr()  # drain first call output
    rc = _cmd_trust(str(cfg), refresh=False)
    assert rc == 0
    assert "already trusted" in capsys.readouterr().out


def test_cmd_trust_refresh_updates_checksum(tmp_path, isolated_trust_store, capsys):
    cfg = _write_config(tmp_path)
    _cmd_trust(str(cfg), refresh=False)
    first = json.loads(isolated_trust_store.read_text())
    first_sum = first["allowed"][str(cfg.resolve())]["checksum"]

    cfg.write_text(cfg.read_text() + "# mutated\n")
    _cmd_trust(str(cfg), refresh=True)
    second = json.loads(isolated_trust_store.read_text())
    second_sum = second["allowed"][str(cfg.resolve())]["checksum"]

    assert first_sum != second_sum


def test_cmd_trust_missing_config_errors(tmp_path, isolated_trust_store, capsys):
    rc = _cmd_trust(str(tmp_path / "nope.yml"), refresh=False)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


# ---- run_pipeline gate ---------------------------------------------------


def test_pipeline_returns_untrusted_status(tmp_path, isolated_trust_store):
    cfg = _write_config(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello\n")

    result = run_pipeline(str(cfg), str(target), "")
    assert result["status"] == "untrusted"
    assert result["trust_status"] == "untrusted"
    assert str(cfg.resolve()) in result["config"]


def test_pipeline_runs_after_trust(tmp_path, isolated_trust_store):
    cfg = _write_config(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello\n")

    _cmd_trust(str(cfg), refresh=False)
    result = run_pipeline(str(cfg), str(target), "")
    assert result["status"] in ("pass",)


def test_pipeline_blocks_after_config_mutation(tmp_path, isolated_trust_store):
    cfg = _write_config(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello\n")

    _cmd_trust(str(cfg), refresh=False)
    cfg.write_text(cfg.read_text() + "# tweak\n")
    result = run_pipeline(str(cfg), str(target), "")
    assert result["status"] == "untrusted"
    assert result["trust_status"] == "mismatch"


def test_pipeline_env_override_skips_gate(tmp_path, monkeypatch, isolated_trust_store):
    cfg = _write_config(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello\n")

    monkeypatch.setenv("BULLY_TRUST_ALL", "1")
    result = run_pipeline(str(cfg), str(target), "")
    assert result["status"] != "untrusted"


# ---- hook_mode integration ----------------------------------------------


def test_hook_mode_untrusted_writes_stderr_and_exits_zero(
    tmp_path, monkeypatch, isolated_trust_store, capsys
):
    cfg = _write_config(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello\n")

    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "hello",
            "new_string": "hello2",
        },
    }
    import bully.cli.hook_mode as hm

    monkeypatch.setattr(hm, "read_stdin_payload", lambda: payload)
    monkeypatch.setattr(hm, "find_config_upward", lambda _p: cfg)

    rc = hm.run_hook_mode()
    captured = capsys.readouterr()
    assert rc == 0
    assert "not trusted" in captured.err
    assert "bully trust" in captured.err
    # Hook-mode must not emit a semantic evaluation payload on stdout.
    assert captured.out.strip() == ""
