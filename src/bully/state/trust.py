"""Trust boundary: per-machine allowlist for .bully.yml configs.

A `.bully.yml` can execute arbitrary shell commands via `engine: script`
rules. Cloning a repo with a malicious `.bully.yml` and making any edit
would run attacker-controlled code in the developer's shell. The trust
gate prevents this: the first time bully sees a config on a given machine,
it refuses to execute any rules until the user runs `bully trust`. After
trust, the gate verifies the checksum on every run -- any change to the
config (or any extended config) re-requires explicit trust.

Trust state is machine-local (`~/.bully-trust.json`), never committed to
repos. `BULLY_TRUST_ALL=1` bypasses the gate for CI and first-time setup
scripts that have already reviewed the config through other means.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bully.config.loader import collect_config_files

_TRUST_ENV_VAR = "BULLY_TRUST_ALL"


def trust_store_path() -> Path:
    """Per-machine allowlist location."""
    override = os.environ.get("BULLY_TRUST_STORE")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".bully-trust.json"


def config_checksum(config_path: str) -> str:
    """SHA256 over the concatenated bytes of a config and all its `extends:` targets.

    Returns '' when the top-level config is unreadable.
    """
    files = collect_config_files(config_path)
    if not files:
        return ""
    h = hashlib.sha256()
    for f in files:
        try:
            h.update(f.read_bytes())
            # Domain separator prevents collisions across different file splits.
            h.update(b"\x00")
        except OSError:
            return ""
    return h.hexdigest()


def load_trust_store() -> dict:
    """Parse the trust store. Returns {} on any read or parse error."""
    p = trust_store_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_trust_store(store: dict) -> None:
    """Write the trust store, creating parent dirs as needed."""
    p = trust_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def trust_status(config_path: str) -> tuple[str, str]:
    """Return (status, detail). Status is one of: 'trusted', 'untrusted', 'mismatch'.

    'untrusted' means the config has never been trusted on this machine.
    'mismatch' means it was trusted, but the contents have since changed.
    """
    if os.environ.get(_TRUST_ENV_VAR) == "1":
        return "trusted", "env:BULLY_TRUST_ALL"
    abs_path = str(Path(config_path).resolve())
    checksum = config_checksum(abs_path)
    if not checksum:
        return "untrusted", "cannot read config"
    store = load_trust_store()
    entry = store.get("allowed", {}).get(abs_path)
    if not isinstance(entry, dict):
        return "untrusted", "never trusted"
    recorded = entry.get("checksum", "")
    if recorded != checksum:
        return "mismatch", f"checksum changed (was {recorded[:12]}..., now {checksum[:12]}...)"
    return "trusted", recorded[:12] + "..."


def untrusted_stderr(config_path: str, status: str, detail: str) -> str:
    """Rendered stderr message for untrusted/mismatched configs."""
    abs_path = Path(config_path).resolve()
    if status == "mismatch":
        headline = f"bully: {abs_path} changed since last trust ({detail})."
        action = "Re-review the config, then run: bully trust --refresh"
    else:
        headline = f"bully: {abs_path} is not trusted on this machine."
        action = "Review the config, then run: bully trust"
    return (
        f"{headline}\n"
        f"Scripts in .bully.yml execute on your machine. "
        f"Until trusted, rules will not run. Edits are not blocked.\n"
        f"{action}\n"
        f"(To allow all configs unconditionally -- not recommended -- "
        f"set {_TRUST_ENV_VAR}=1.)\n"
    )


def cmd_trust(config_path: str | None, refresh: bool) -> int:
    """Record the current config's checksum in the trust store."""
    path = config_path or ".bully.yml"
    abs_path = Path(path).resolve()
    if not abs_path.is_file():
        print(f"config not found: {abs_path}", file=sys.stderr)
        return 1
    checksum = config_checksum(str(abs_path))
    if not checksum:
        print(f"cannot checksum config at {abs_path}", file=sys.stderr)
        return 1

    store = load_trust_store()
    allowed = store.setdefault("allowed", {})
    existing = allowed.get(str(abs_path))
    if isinstance(existing, dict) and existing.get("checksum") == checksum and not refresh:
        print(f"already trusted: {abs_path}  sha256={checksum[:12]}...")
        return 0
    allowed[str(abs_path)] = {
        "checksum": checksum,
        "allowed_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    save_trust_store(store)
    verb = "updated" if existing else "trusted"
    print(f"{verb}: {abs_path}  sha256={checksum[:12]}...")
    return 0
