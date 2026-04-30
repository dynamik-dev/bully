"""Tests for baseline filtering and --baseline-init.

Covers:
- baseline.json format
- filtering violations by baseline checksum
- un-baselining when a line moves (checksum mismatch)
- --baseline-init writes the expected file shape
"""

import hashlib
import json
import subprocess
import sys

from bully import run_pipeline

RULE_YAML = (
    "rules:\n"
    "  no-foo:\n"
    '    description: "forbidden pattern"\n'
    "    engine: script\n"
    '    scope: "*.py"\n'
    "    severity: error\n"
    "    script: \"grep -n 'FORBIDDEN' {file} && exit 1 || exit 0\"\n"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_baselined_violation_is_filtered(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir(parents=True)
    line_content = "x = 'FORBIDDEN'\n"
    target.write_text(f"def hello():\n    {line_content}")

    baseline_dir = tmp_path / ".bully"
    baseline_dir.mkdir()
    # line 2 has the forbidden text
    full_line = f"    {line_content}"
    baseline = {
        "baseline": [
            {
                "rule_id": "no-foo",
                "file": "src/a.py",
                "line": 2,
                "checksum": _sha(full_line),
            }
        ]
    }
    (baseline_dir / "baseline.json").write_text(json.dumps(baseline))

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    # The one violation is baselined -> status should be pass
    assert result["status"] == "pass", f"expected pass, got: {result}"


def test_unbaselined_when_checksum_changes(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "src" / "b.py"
    target.parent.mkdir(parents=True)
    target.write_text("def hello():\n    x = 'FORBIDDEN'\n")

    baseline_dir = tmp_path / ".bully"
    baseline_dir.mkdir()
    baseline = {
        "baseline": [
            {
                "rule_id": "no-foo",
                "file": "src/b.py",
                "line": 2,
                # wrong checksum on purpose -- simulates the line having moved/changed
                "checksum": "deadbeef" * 8,
            }
        ]
    }
    (baseline_dir / "baseline.json").write_text(json.dumps(baseline))

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    assert result["status"] == "blocked"
    assert any(v["rule"] == "no-foo" for v in result["violations"])


def test_baseline_only_matches_same_rule(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "src" / "c.py"
    target.parent.mkdir(parents=True)
    target.write_text("def hello():\n    x = 'FORBIDDEN'\n")

    baseline_dir = tmp_path / ".bully"
    baseline_dir.mkdir()
    full_line = "    x = 'FORBIDDEN'\n"
    baseline = {
        "baseline": [
            {
                "rule_id": "some-other-rule",  # different rule
                "file": "src/c.py",
                "line": 2,
                "checksum": _sha(full_line),
            }
        ]
    }
    (baseline_dir / "baseline.json").write_text(json.dumps(baseline))

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    assert result["status"] == "blocked"


def test_baseline_init_writes_file(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "src" / "d.py"
    target.parent.mkdir(parents=True)
    target.write_text("def hello():\n    x = 'FORBIDDEN'\n")

    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "bully",
            "--baseline-init",
            "--config",
            str(tmp_path / ".bully.yml"),
            "--glob",
            "**/*.py",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 0, f"stderr={r.stderr}"
    bl = tmp_path / ".bully" / "baseline.json"
    assert bl.is_file(), f"baseline.json missing; stdout={r.stdout}"
    data = json.loads(bl.read_text())
    assert "baseline" in data
    assert isinstance(data["baseline"], list)
    assert len(data["baseline"]) >= 1
    entry = data["baseline"][0]
    assert entry["rule_id"] == "no-foo"
    assert entry["file"].endswith("d.py")
    assert entry["line"] == 2
    assert len(entry["checksum"]) == 64  # sha256 hex


def test_baseline_init_then_subsequent_run_passes(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "src" / "e.py"
    target.parent.mkdir(parents=True)
    target.write_text("def hello():\n    x = 'FORBIDDEN'\n")

    # initial: baseline everything
    subprocess.run(
        [
            sys.executable,
            "-m",
            "bully",
            "--baseline-init",
            "--config",
            str(tmp_path / ".bully.yml"),
            "--glob",
            "**/*.py",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    # second run: should pass because the violation is in the baseline
    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    assert result["status"] == "pass"


def test_no_baseline_json_means_no_filtering(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "src" / "f.py"
    target.parent.mkdir(parents=True)
    target.write_text("def hello():\n    x = 'FORBIDDEN'\n")

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    assert result["status"] == "blocked"


def test_malformed_baseline_json_treated_as_empty(tmp_path):
    (tmp_path / ".bully.yml").write_text(RULE_YAML)
    target = tmp_path / "src" / "g.py"
    target.parent.mkdir(parents=True)
    target.write_text("def hello():\n    x = 'FORBIDDEN'\n")

    bd = tmp_path / ".bully"
    bd.mkdir()
    (bd / "baseline.json").write_text("{not valid json")

    result = run_pipeline(
        str(tmp_path / ".bully.yml"),
        str(target),
        "",
    )
    # Parser returns empty dict for malformed JSON; no filtering.
    assert result["status"] == "blocked"
