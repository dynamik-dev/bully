"""Tests for the `extends:` resolution in parse_config.

Covers: happy-path inherit + merge, local override, cycle detection,
relative + absolute paths, and missing files.
"""

from pathlib import Path

import pytest

from bully import ConfigError, parse_config

RULE_BASE = (
    "rules:\n"
    "  from-base:\n"
    '    description: "from the base pack"\n'
    "    engine: script\n"
    '    scope: "*.py"\n'
    "    severity: error\n"
    '    script: "exit 0"\n'
)

RULE_OTHER = (
    "rules:\n"
    "  from-other:\n"
    '    description: "other pack rule"\n'
    "    engine: script\n"
    '    scope: "*.ts"\n'
    "    severity: warning\n"
    '    script: "exit 0"\n'
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_extends_relative_path_pulls_rules(tmp_path):
    _write(tmp_path / "base.yml", RULE_BASE)
    cfg = tmp_path / ".bully.yml"
    _write(
        cfg,
        'extends: ["./base.yml"]\n' + "rules:\n",
    )
    rules = parse_config(str(cfg))
    ids = [r.id for r in rules]
    assert "from-base" in ids
    assert rules[0].engine == "script"
    assert rules[0].severity == "error"


def test_local_overrides_inherited(tmp_path, capsys):
    _write(tmp_path / "base.yml", RULE_BASE)
    cfg = tmp_path / ".bully.yml"
    _write(
        cfg,
        'extends: ["./base.yml"]\n'
        "rules:\n"
        "  from-base:\n"
        '    description: "local override"\n'
        "    engine: script\n"
        '    scope: "*.py"\n'
        "    severity: warning\n"
        '    script: "exit 1"\n',
    )
    rules = parse_config(str(cfg))
    by_id = {r.id: r for r in rules}
    assert by_id["from-base"].severity == "warning"
    assert by_id["from-base"].description == "local override"
    # only one entry (no duplicate)
    assert sum(1 for r in rules if r.id == "from-base") == 1
    captured = capsys.readouterr()
    assert "overridden" in captured.err


def test_extends_cycle_raises(tmp_path):
    a = tmp_path / "a.yml"
    b = tmp_path / "b.yml"
    _write(a, 'extends: ["./b.yml"]\nrules:\n')
    _write(b, 'extends: ["./a.yml"]\nrules:\n')
    with pytest.raises(ConfigError) as exc_info:
        parse_config(str(a))
    assert "cycle" in str(exc_info.value).lower()


def test_extends_absolute_path(tmp_path):
    abs_base = tmp_path / "elsewhere" / "base.yml"
    _write(abs_base, RULE_BASE)
    cfg = tmp_path / "project" / ".bully.yml"
    _write(cfg, f'extends: ["{abs_base}"]\nrules:\n')
    rules = parse_config(str(cfg))
    assert any(r.id == "from-base" for r in rules)


def test_extends_missing_file_raises(tmp_path):
    cfg = tmp_path / ".bully.yml"
    _write(cfg, 'extends: ["./does-not-exist.yml"]\nrules:\n')
    with pytest.raises(ConfigError) as exc_info:
        parse_config(str(cfg))
    assert "not found" in str(exc_info.value).lower()


def test_multiple_extends_merge_ordered(tmp_path):
    _write(tmp_path / "a.yml", RULE_BASE)
    _write(tmp_path / "b.yml", RULE_OTHER)
    cfg = tmp_path / ".bully.yml"
    _write(cfg, 'extends: ["./a.yml", "./b.yml"]\nrules:\n')
    rules = parse_config(str(cfg))
    ids = [r.id for r in rules]
    assert "from-base" in ids
    assert "from-other" in ids


def test_extends_block_list_form(tmp_path):
    _write(tmp_path / "a.yml", RULE_BASE)
    _write(tmp_path / "b.yml", RULE_OTHER)
    cfg = tmp_path / ".bully.yml"
    _write(
        cfg,
        "extends:\n  - ./a.yml\n  - ./b.yml\nrules:\n",
    )
    rules = parse_config(str(cfg))
    ids = {r.id for r in rules}
    assert ids == {"from-base", "from-other"}


def test_transitive_extends(tmp_path):
    _write(tmp_path / "c.yml", RULE_BASE)
    _write(tmp_path / "b.yml", 'extends: ["./c.yml"]\nrules:\n')
    cfg = tmp_path / "a.yml"
    _write(cfg, 'extends: ["./b.yml"]\nrules:\n')
    rules = parse_config(str(cfg))
    assert [r.id for r in rules] == ["from-base"]
