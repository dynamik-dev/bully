"""Git + Anthropic SDK metadata for bench history records."""

from __future__ import annotations

import subprocess

from bully.bench.dispatch import import_anthropic


def git_sha() -> str | None:
    """Best-effort current git SHA; None if git unavailable or not a repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def git_dirty() -> bool:
    """True iff there are uncommitted changes."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return bool(r.stdout.strip())


def anthropic_sdk_version() -> str | None:
    mod = import_anthropic()
    return getattr(mod, "__version__", None) if mod else None
