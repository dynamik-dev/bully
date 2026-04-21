"""Tests for parsing the top-level `execution:` block in .bully.yml."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import ConfigError, _parse_single_file


def _write(tmp_path, body):
    p = tmp_path / ".bully.yml"
    p.write_text(body)
    return str(p)


def test_execution_block_absent_yields_none(tmp_path):
    body = (
        "rules:\n"
        "  r1:\n"
        "    description: x\n"
        "    engine: script\n"
        "    scope: '*'\n"
        "    severity: error\n"
        "    script: 'true'\n"
    )
    parsed = _parse_single_file(_write(tmp_path, body))
    assert parsed.max_workers is None


def test_execution_block_sets_max_workers(tmp_path):
    body = (
        "execution:\n"
        "  max_workers: 4\n"
        "rules:\n"
        "  r1:\n"
        "    description: x\n"
        "    engine: script\n"
        "    scope: '*'\n"
        "    severity: error\n"
        "    script: 'true'\n"
    )
    parsed = _parse_single_file(_write(tmp_path, body))
    assert parsed.max_workers == 4


def test_execution_block_unknown_subkey_raises(tmp_path):
    body = "execution:\n  bogus: 1\n"
    with pytest.raises(ConfigError, match="unknown execution field"):
        _parse_single_file(_write(tmp_path, body))


def test_execution_block_non_positive_raises(tmp_path):
    body = "execution:\n  max_workers: 0\n"
    with pytest.raises(ConfigError, match="max_workers must be a positive integer"):
        _parse_single_file(_write(tmp_path, body))


def test_execution_block_non_integer_raises(tmp_path):
    body = "execution:\n  max_workers: abc\n"
    with pytest.raises(ConfigError, match="max_workers must be a positive integer"):
        _parse_single_file(_write(tmp_path, body))
