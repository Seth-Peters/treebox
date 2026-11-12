"""The host-owned worktree record: the authority for a worktree's creation policy.

A sandboxed agent can write its worktree and the repo's git common dir (both
are bind-mounted), so nothing it can reach may decide how its *next* session
runs. The policy a worktree was created with - isolation, firewall, sandbox
template, harness, base, and the effective resource limits - therefore lives
here, in a file outside every sandbox mount, beside the lock and the docker runner's config dir::

    <worktree parent>/.records/<worktree leaf>.json

Only host-side treebox writes it, and only under the worktree lock: ``create``
writes it before any runner resource depends on it, and ``adopt`` (the
operator's explicit migration step) writes it for a worktree that has none.
The sandbox-writable state in ``state.py`` keeps only observations (lockfile
hash, setup progress) and is never promoted to authority.

The record is bound to the registered worktree path it describes: a record
whose ``worktree`` names another path (a copied or moved file) is invalid.
A missing or invalid record is a ``RecordError``; callers surface it as a
classified error with a recovery path and never fall back to config defaults.

Module API (what later features extend)::

    rec = record.load(path)                    # WorktreeRecord, or RecordError
    record.save(dataclasses.replace(rec, ...)) # atomic replace
    record.remove(path)                        # on teardown

Extending it: add a field to ``WorktreeRecord``, bump ``SCHEMA_VERSION``, and
teach ``_parse`` to read (and upgrade) every older supported version. Unknown
keys and unknown versions are rejected, so an older treebox never silently
drops a newer policy field - it refuses the record instead.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import is_valid_template_name, same_path
from .resources import ResourceLimitError, ResourceLimits, from_mapping

# 2 added ``resources`` (the effective CPU/memory/process limits). A version-1
# record has no limits and still loads: it means "no treebox cap".
SCHEMA_VERSION = 2
_SUPPORTED_VERSIONS = (1, 2)

# Sibling of the worktrees under one parent, like `.locks/` and `.containers/`.
RECORD_DIR = ".records"

_V1_KEYS = frozenset(
    {"schemaVersion", "worktree", "base", "isolation", "harness", "firewall", "template"}
)
_KEYS = _V1_KEYS | {"resources"}


@dataclass(frozen=True)
class WorktreeRecord:
    """The creation policy of the worktree at ``worktree`` (its registered path).

    ``isolation`` and ``harness`` are registry names (validated against the
    registries by the CLI, like every other boundary string); ``template`` is
    a template identifier (``models.is_valid_template_name``); ``firewall`` is
    always a real bool (``False`` for host worktrees). ``resources`` are the
    limits the sandbox was created with (unset = no treebox cap; always unset
    for a runner that applies none, such as host)."""

    worktree: Path
    base: str
    isolation: str
    harness: str
    firewall: bool
    template: str
    resources: ResourceLimits = field(default_factory=ResourceLimits)


class RecordError(RuntimeError):
    """The record for ``worktree`` cannot be used. ``path`` is the record file."""

    def __init__(self, worktree: Path, path: Path, message: str) -> None:
        super().__init__(message)
        self.worktree = worktree
        self.path = path


class RecordMissingError(RecordError):
    """No record exists: a worktree from an older treebox (legacy), one whose
    record was deleted, or one created outside treebox."""


class RecordInvalidError(RecordError):
    """A record exists but cannot be trusted: unreadable, malformed, an
    unsupported ``schemaVersion``, or bound to a different worktree path."""


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
    if type(version) is not int or version not in _SUPPORTED_VERSIONS:
        raise _invalid(
            worktree, path, f"unsupported schemaVersion {version!r} (expected {SCHEMA_VERSION})"
        )
    keys = _V1_KEYS if version == 1 else _KEYS
    unknown = sorted(set(data) - keys)
    if unknown:
        raise _invalid(worktree, path, f"unknown key(s) {', '.join(unknown)}")
    missing = sorted(keys - set(data))
    if missing:
        raise _invalid(worktree, path, f"missing key(s) {', '.join(missing)}")
    for key in ("worktree", "base", "isolation", "harness", "template"):
        if not isinstance(data[key], str):
            raise _invalid(worktree, path, f"{key} must be a string")
    if type(data["firewall"]) is not bool:
        raise _invalid(worktree, path, "firewall must be true or false")
    if not is_valid_template_name(data["template"]):
        raise _invalid(worktree, path, f"template {data['template']!r} is not a template name")
    if not same_path(data["worktree"], worktree):
        raise _invalid(worktree, path, f"it is bound to worktree {data['worktree']!r}")
    try:
        # Version 1 predates limits: no treebox cap.
        limits = from_mapping(data.get("resources", {}), source="resources")
    except ResourceLimitError as exc:
        raise _invalid(worktree, path, str(exc).rstrip(".")) from exc
    return WorktreeRecord(
        worktree=worktree,
        base=data["base"],
        isolation=data["isolation"],
        harness=data["harness"],
        firewall=data["firewall"],
        template=data["template"],
        resources=limits,
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
