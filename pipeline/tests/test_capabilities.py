"""Tests for capability-scoped script execution."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import _capability_env, parse_config

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = REPO_ROOT / "pipeline" / "pipeline.py"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PIPELINE), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_capabilities_field_parses(tmp_path):
    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  net-rule:
    description: x
    severity: error
    engine: script
    scope: ['**']
    script: 'true'
    capabilities:
      network: false
      writes: cwd-only
"""
    )
    rules = parse_config(str(cfg))
    rule = next(r for r in rules if r.id == "net-rule")
    assert rule.capabilities == {"network": False, "writes": "cwd-only"}


def test_capabilities_network_false_strips_proxy_env():
    """When network: false is declared, the script subprocess should not see HTTP_PROXY etc."""
    base_env = {
        "HTTP_PROXY": "http://x",
        "HTTPS_PROXY": "http://y",
        "ALL_PROXY": "http://z",
        "PATH": "/usr/bin",
    }
    out = _capability_env(base_env, {"network": False, "writes": "cwd-only"})
    assert "HTTP_PROXY" not in out
    assert "HTTPS_PROXY" not in out
    assert "ALL_PROXY" not in out
    assert out["NO_PROXY"] == "*"
    assert out["PATH"] == "/usr/bin"


def test_capabilities_default_is_unrestricted():
    base_env = {"HTTP_PROXY": "http://x", "PATH": "/usr/bin"}
    out = _capability_env(base_env, None)
    assert out == base_env


def test_script_rule_subprocess_sees_capability_modified_env(tmp_path, monkeypatch):
    """End-to-end: a rule with `network: false` runs a script that prints HTTP_PROXY.

    The captured stdout must show the variable as unset, proving the env shim
    is wired into execute_script_rule. Regression guard for the integration
    point — without this test, dropping `env=...` from the subprocess call
    would not fail any test.
    """
    monkeypatch.setenv("HTTP_PROXY", "http://upstream-proxy.local:8080")

    cfg = tmp_path / ".bully.yml"
    cfg.write_text(
        """
rules:
  net-blocked:
    description: tripwire on accidental network use
    severity: error
    engine: script
    scope: ['**/*.py']
    script: 'echo "HTTP_PROXY=${HTTP_PROXY:-UNSET}"; test -z "${HTTP_PROXY}"'
    capabilities:
      network: false
"""
    )
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n")

    # Trust the config so script execution isn't gated.
    p_trust = _run(["--trust", "--config", str(cfg)], tmp_path)
    assert p_trust.returncode == 0, (p_trust.stdout, p_trust.stderr)

    p = _run(
        ["--config", str(cfg), "--file", str(target), "--diff", "+ print('hi')"],
        tmp_path,
    )
    # If env was NOT shimmed, HTTP_PROXY would be set and `test -z` exits 1
    # (script verdict = violation). With the shim, HTTP_PROXY is unset and
    # exits 0 (script verdict = pass).
    assert p.returncode == 0, (p.stdout, p.stderr)
