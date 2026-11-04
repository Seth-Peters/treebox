"""Read the claude-lock.json that `ant apply` writes.

We only read it: `ant apply` owns the file. Its resource keys are paths
relative to the directory `ant apply` ran in (factory/), spelled `./<path>`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory_cli.errors import FactoryError

LOCKFILE_NAME = "claude-lock.json"


@dataclass(frozen=True)
class LockedResource:
    kind: str
    id: str
    version: int | None


@dataclass(frozen=True)
class Lockfile:
    resources: dict[str, LockedResource]

    def get(self, relative_path: str, kind: str) -> LockedResource:
        key = relative_path if relative_path.startswith("./") else f"./{relative_path}"
        found = self.resources.get(key)
        if found is None:
            raise FactoryError(
                f"{LOCKFILE_NAME} has no entry for {key}; run `ant apply` from factory/ first"
            )
        if found.kind != kind:
            raise FactoryError(f"{LOCKFILE_NAME}: {key} is a {found.kind}, expected {kind}")
        return found


def _parse_version(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise FactoryError(f"{LOCKFILE_NAME}: unreadable version {raw!r}") from exc


def parse_lockfile(data: Any) -> Lockfile:
    if not isinstance(data, dict) or not isinstance(data.get("resources"), dict):
        raise FactoryError(f"{LOCKFILE_NAME} has no `resources` mapping")
    resources: dict[str, LockedResource] = {}
    for key, entry in data["resources"].items():
        if not isinstance(entry, dict) or "id" not in entry or "kind" not in entry:
            raise FactoryError(f"{LOCKFILE_NAME}: malformed entry for {key}")
        resources[key] = LockedResource(
            kind=str(entry["kind"]),
            id=str(entry["id"]),
            version=_parse_version(entry.get("version")),
        )
    return Lockfile(resources=resources)


def load_lockfile(factory_dir: Path) -> Lockfile:
    path = factory_dir / LOCKFILE_NAME
    try:
        return parse_lockfile(json.loads(path.read_text()))
    except FileNotFoundError as exc:
        raise FactoryError(
            f"no {LOCKFILE_NAME} in {factory_dir}; run `ant apply .` from factory/ first"
        ) from exc
    except json.JSONDecodeError as exc:
        raise FactoryError(f"{path}: invalid JSON: {exc}") from exc
