"""Tests for `bully guide` and `bully explain` scoped feedforward commands."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bully", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  php-only:
    description: PHP rule
    severity: error
    engine: script
    scope: ['**/*.php']
    script: 'true'
  any-file:
    description: Any file rule
    severity: warning
    engine: script
    scope: ['**']
    script: 'true'
  ts-only:
    description: TS rule
    severity: error
    engine: script
    scope: ['**/*.ts']
    script: 'true'
"""
    )
    (tmp_path / "src").mkdir()
    return tmp_path


def test_guide_lists_only_in_scope_rules(tmp_path):
    repo = _make_repo(tmp_path)
    p = _run(["guide", "src/foo.php"], repo)
    assert p.returncode == 0, p.stderr
    assert "php-only" in p.stdout
    assert "any-file" in p.stdout
    assert "ts-only" not in p.stdout


def test_explain_includes_match_reasoning(tmp_path):
    repo = _make_repo(tmp_path)
    p = _run(["explain", "src/foo.php"], repo)
    assert p.returncode == 0, p.stderr
    assert "php-only" in p.stdout
    # Explain should say *why* -- i.e., show the matching glob.
    assert "**/*.php" in p.stdout


def test_guide_with_only_globally_scoped_rule_matches_any_file(tmp_path):
    repo = _make_repo(tmp_path)
    p = _run(["guide", "README.md"], repo)
    assert p.returncode == 0
    # Only `any-file` (scope `**`) matches a top-level non-source file.
    assert "any-file" in p.stdout


def test_guide_no_matching_rules_prints_no_rules_message(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  ts-only:
    description: TS rule
    severity: error
    engine: script
    scope: ['**/*.ts']
    script: 'true'
"""
    )
    p = _run(["guide", "README.md"], tmp_path)
    assert p.returncode == 0
    assert "No bully rules apply" in p.stdout
