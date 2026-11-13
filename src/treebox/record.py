"""The host-owned worktree record: the authority for a worktree's creation policy.

A sandboxed agent can write its worktree and the repo's git common dir (both
are bind-mounted), so nothing it can reach may decide how its *next* session
runs. The policy a worktree was created with - isolation, firewall, sandbox
template, harness, base, and the effective resource limits - therefore lives
here, in a file outside every sandbox mount, beside the lock and the docker runner's config dir::

    <worktree parent>/.records/<worktree leaf>.json

Only host-side treebox writes it: ``create`` writes it under the worktree
lock, before any runner resource depends on it. The sandbox-writable state in
``state.py`` keeps only observations (lockfile hash, setup progress) and is
never promoted to authority.

The record is bound to the registered worktree path it describes: a record
whose ``worktree`` names another path (a copied or moved file) is invalid.
A missing or invalid record is a ``RecordError``; callers surface it as a
classified error whose recovery is to tear the worktree down and create it
again, and never fall back to config defaults.

Module API (what later features extend)::

    rec = record.load(path)                    # WorktreeRecord, or RecordError
    record.save(dataclasses.replace(rec, ...)) # atomic replace
    record.remove(path)                        # on teardown

There is one schema version and no upgrade path. The record format has never
been released, so a new field is a new required key of that one version: add
it to ``WorktreeRecord`` and do not bump ``SCHEMA_VERSION``. A record with
another version or an unknown key is invalid, so a worktree made by another
treebox version is torn down and created again, never reinterpreted.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .harnesses import VALID_HARNESSES
from .models import is_valid_template_name, same_path
from .resources import RESOURCE_NAMES, ResourceLimitError, ResourceLimits, from_mapping
from .runners import VALID_ISOLATION

SCHEMA_VERSION = 1

# Sibling of the worktrees under one parent, like `.locks/` and `.containers/`.
RECORD_DIR = ".records"

_KEYS = frozenset(
    {
        "schemaVersion",
        "worktree",
        "base",
        "isolation",
        "harness",
        "firewall",
        "template",
        "resources",
        "explicitResources",
    }
)


@dataclass(frozen=True)
class WorktreeRecord:
    """The creation policy of the worktree at ``worktree`` (its registered path).

    ``isolation`` and ``harness`` are registry names; ``template`` is a
    template identifier (``models.is_valid_template_name``); ``firewall`` is
    always a real bool (``False`` for host worktrees). ``resources`` are the
    limits the sandbox was created with (unset = no treebox cap; always unset
    for a runner that applies none, such as host). ``explicit_resources``
    names the limits an explicit ``create`` flag chose; every other limit came
    from config or the template, so only those can drift."""

    worktree: Path
    base: str
    isolation: str
    harness: str
    firewall: bool
    template: str
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    explicit_resources: frozenset[str] = frozenset()


class RecordError(RuntimeError):
    """The record for ``worktree`` cannot be used. ``path`` is the record file."""

    def __init__(self, worktree: Path, path: Path, message: str) -> None:
        super().__init__(message)
        self.worktree = worktree
        self.path = path


class RecordMissingError(RecordError):
    """No record exists: a worktree made by another treebox version, one
    whose record was deleted, or one created outside treebox."""


class RecordInvalidError(RecordError):
    """A record exists but cannot be trusted: unreadable, malformed, another
    ``schemaVersion``, an unknown isolation mode or harness, or bound to a
    different worktree path."""


def record_path(worktree: Path) -> Path:
    """Where the record for the worktree at ``worktree`` lives."""
    return worktree.parent / RECORD_DIR / f"{worktree.name}.json"


def exists(worktree: Path) -> bool:
    """Whether any record file (valid or not) is present for ``worktree``."""
    return os.path.lexists(record_path(worktree))


def load(worktree: Path) -> WorktreeRecord:
    """Read and validate the record for ``worktree``. Raises ``RecordError``."""
    path = record_path(worktree)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecordMissingError(
            worktree, path, f"Worktree '{worktree.name}' has no host-owned record ({path})."
        ) from None
    except OSError as exc:
        raise RecordInvalidError(
            worktree, path, f"Cannot read the record of worktree '{worktree.name}' ({path}): {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _invalid(worktree, path, f"invalid JSON: {exc}") from exc
    return _parse(worktree, path, data)


def _invalid(worktree: Path, path: Path, reason: str) -> RecordInvalidError:
    return RecordInvalidError(
        worktree, path, f"The record of worktree '{worktree.name}' ({path}) is invalid: {reason}."
    )


def _parse(worktree: Path, path: Path, data: Any) -> WorktreeRecord:
    if not isinstance(data, dict):
        raise _invalid(worktree, path, "not a JSON object")
    version = data.get("schemaVersion")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise _invalid(
            worktree, path, f"unsupported schemaVersion {version!r} (expected {SCHEMA_VERSION})"
        )
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise _invalid(worktree, path, f"unknown key(s) {', '.join(unknown)}")
    missing = sorted(_KEYS - set(data))
    if missing:
        raise _invalid(worktree, path, f"missing key(s) {', '.join(missing)}")
    for key in ("worktree", "base", "isolation", "harness", "template"):
        if not isinstance(data[key], str):
            raise _invalid(worktree, path, f"{key} must be a string")
    if data["isolation"] not in VALID_ISOLATION:
        raise _invalid(worktree, path, f"unknown isolation mode {data['isolation']!r}")
    if data["harness"] not in VALID_HARNESSES:
        raise _invalid(worktree, path, f"unknown harness {data['harness']!r}")
    if type(data["firewall"]) is not bool:
        raise _invalid(worktree, path, "firewall must be true or false")
    if not is_valid_template_name(data["template"]):
        raise _invalid(worktree, path, f"template {data['template']!r} is not a template name")
    if not same_path(data["worktree"], worktree):
        raise _invalid(worktree, path, f"it is bound to worktree {data['worktree']!r}")
    try:
        limits = from_mapping(data["resources"], source="resources")
    except ResourceLimitError as exc:
        raise _invalid(worktree, path, str(exc).rstrip(".")) from exc
    explicit = data["explicitResources"]
    if (
        not isinstance(explicit, list)
        or not all(isinstance(n, str) for n in explicit)
        or len(set(explicit)) != len(explicit)
    ):
        raise _invalid(worktree, path, "explicitResources must be a list of distinct limit names")
    # A flag always sets a value, so an explicit limit is a recorded one.
    stray = sorted(set(explicit) - set(limits.names()))
    if stray:
        raise _invalid(worktree, path, f"explicitResources names unset limit(s) {', '.join(stray)}")
    return WorktreeRecord(
        worktree=worktree,
        base=data["base"],
        isolation=data["isolation"],
        harness=data["harness"],
        firewall=data["firewall"],
        template=data["template"],
        resources=limits,
        explicit_resources=frozenset(explicit),
    )


def save(rec: WorktreeRecord) -> None:
    """Write ``rec`` atomically: a complete temp file in the same directory,
    flushed, then renamed over the record. A reader sees the old record or
    the new one, never a partial file. Callers hold the worktree lock."""
    path = record_path(rec.worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "worktree": str(rec.worktree),
        "base": rec.base,
        "isolation": rec.isolation,
        "harness": rec.harness,
        "firewall": rec.firewall,
        "template": rec.template,
        "resources": rec.resources.to_json(),
        "explicitResources": [n for n in RESOURCE_NAMES if n in rec.explicit_resources],
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def remove(worktree: Path) -> None:
    """Delete the record for ``worktree`` (teardown). Missing is fine."""
    with contextlib.suppress(FileNotFoundError):
        record_path(worktree).unlink()
