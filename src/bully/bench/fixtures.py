"""Bench fixtures: directory layout (`config.yml` + `fixture.json`) and loaders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class FixtureError(Exception):
    """Raised when a fixture directory is malformed."""


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    file_path: str
    edit_type: str
    diff: str
    config_path: Path

    @property
    def dir(self) -> Path:
        return self.config_path.parent


def load_fixture(fixture_dir: Path) -> Fixture:
    """Load a fixture from `<dir>/config.yml` + `<dir>/fixture.json`."""
    fixture_dir = Path(fixture_dir)
    cfg = fixture_dir / "config.yml"
    meta = fixture_dir / "fixture.json"
    if not cfg.is_file():
        raise FixtureError(f"missing config.yml in {fixture_dir}")
    if not meta.is_file():
        raise FixtureError(f"missing fixture.json in {fixture_dir}")
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FixtureError(f"malformed fixture.json in {fixture_dir}: {e}") from e

    required = ("name", "description", "file_path", "edit_type", "diff")
    for key in required:
        if key not in data:
            raise FixtureError(f"fixture.json in {fixture_dir} missing field {key!r}")

    return Fixture(
        name=data["name"],
        description=data["description"],
        file_path=data["file_path"],
        edit_type=data["edit_type"],
        diff=data["diff"],
        config_path=cfg,
    )


def discover_fixtures(root: Path) -> list[Fixture]:
    """Load every fixture subdirectory under `root`, sorted by name."""
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[Fixture] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        out.append(load_fixture(child))
    return out
