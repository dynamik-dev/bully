"""Tests for the new `output:` rule field (parsed vs. passthrough)."""

import textwrap
from pathlib import Path

import pytest

from bully import ConfigError, parse_config


def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".bully.yml"
    p.write_text(textwrap.dedent(body))
    return p


def test_output_parsed_is_default(tmp_path):
    cfg = _write_cfg(
        tmp_path,
        """
        rules:
          r:
            description: d
            engine: script
            scope: "*"
            script: "true"
        """,
    )
    rules = parse_config(str(cfg))
    assert rules[0].output_mode == "parsed"


def test_output_passthrough_accepted(tmp_path):
    cfg = _write_cfg(
        tmp_path,
        """
        rules:
          r:
            description: d
            engine: script
            scope: "*"
            script: "true"
            output: passthrough
        """,
    )
    rules = parse_config(str(cfg))
    assert rules[0].output_mode == "passthrough"


def test_output_invalid_value_rejected(tmp_path):
    cfg = _write_cfg(
        tmp_path,
        """
        rules:
          r:
            description: d
            engine: script
            scope: "*"
            script: "true"
            output: weird
        """,
    )
    with pytest.raises(ConfigError, match="output 'weird'"):
        parse_config(str(cfg))


def test_output_only_valid_for_script_engine(tmp_path):
    cfg = _write_cfg(
        tmp_path,
        """
        rules:
          r:
            description: d
            engine: ast
            scope: "*.py"
            pattern: "print($$$)"
            output: passthrough
        """,
    )
    with pytest.raises(ConfigError, match="'output' is only valid when engine is 'script'"):
        parse_config(str(cfg))
