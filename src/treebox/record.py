"""The host-owned worktree record: the authority for a worktree's creation policy.

A sandboxed agent can write its worktree and the repo's git common dir (both
are bind-mounted), so nothing it can reach may decide how its *next* session
runs. The policy a worktree was created with - isolation, firewall, sandbox
template, harness, base - therefore lives here, in a file outside every sandbox
mount, beside the lock and the docker runner's config dir::

    <worktree parent>/.records/<worktree leaf>.json

Only host-side treebox writes it, and only under the worktree lock: ``create``
writes it before any runner resource depends on it, and ``adopt`` (the
operator's explicit migration step) writes it for a worktree that has none.
Nothing in the sandbox-writable git dir is read for any decision; the
``treebox-state.json`` files older treebox versions wrote there are ignored.

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

Besides the policy, the record holds the host-observed result of the last
setup (``setup``, see the setup model in ``lifecycle.py``): a sandbox that
could mark its own setup done could make treebox skip setup forever or report
stale dependencies as fresh, so that fact is host-owned too.

Besides the setup result it keeps a copy of the worktree's port block
(``ports``, see ``ports.py``): the ledger in the treebox home is the one source
of truth for which blocks are taken; the record copy lets ``list`` and the
entry commands read the block without the ledger lock.

Versions: 1 (policy only) is upgraded on read with ``setup`` pending - nothing
says setup finished, so the next ``enter`` runs it again; 2 adds ``setup``;
3 adds ``ports`` (a version 1 or 2 record upgrades with no block, and the next
``enter`` reserves one).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lifecycle import SetupResult, setup_log_path
from .models import PortBlock, is_valid_template_name, same_path

SCHEMA_VERSION = 3

# Sibling of the worktrees under one parent, like `.locks/` and `.containers/`.
RECORD_DIR = ".records"

_V1_KEYS = frozenset(
    {"schemaVersion", "worktree", "base", "isolation", "harness", "firewall", "template"}
)
_V2_KEYS = _V1_KEYS | {"setup"}
_KEYS = _V2_KEYS | {"ports"}


@dataclass(frozen=True)
class WorktreeRecord:
    """The creation policy of the worktree at ``worktree`` (its registered path).

    ``isolation`` and ``harness`` are registry names (validated against the
    registries by the CLI, like every other boundary string); ``template`` is
    a template identifier (``models.is_valid_template_name``); ``firewall`` is
    always a real bool (``False`` for host worktrees). ``setup`` is not
    policy: it is the result of the last setup attempt (``lifecycle.py``). ``ports`` is
    not policy either: the copy of this worktree's ledger entry (``ports.py``),
    None until one is reserved."""

    worktree: Path
    base: str
    isolation: str
    harness: str
    firewall: bool
    template: str
    setup: SetupResult = dataclasses.field(default_factory=SetupResult)
    ports: PortBlock | None = None

    def same_policy(self, other: WorktreeRecord) -> bool:
        """Whether ``other`` states the same creation policy (setup and port
        block aside)."""
        return dataclasses.replace(self, setup=other.setup, ports=other.ports) == other


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
    if type(version) is not int or version not in (1, 2, SCHEMA_VERSION):
        raise _invalid(
            worktree, path, f"unsupported schemaVersion {version!r} (expected {SCHEMA_VERSION})"
        )
    keys = {1: _V1_KEYS, 2: _V2_KEYS}.get(version, _KEYS)
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
    # Version 1 recorded no setup result: pending (never "done"), so the next
    # enter runs setup again instead of trusting anything else.
    setup = SetupResult()
    if version >= 2:
        try:
            setup = SetupResult.from_json(data["setup"])
        except ValueError as exc:
            raise _invalid(worktree, path, str(exc)) from exc
    # Versions 1 and 2 recorded no block: none, and the next enter reserves one
    # (or takes back the ledger's entry for this path).
    ports = _parse_ports(worktree, path, data["ports"]) if version >= 3 else None
    return WorktreeRecord(
        worktree=worktree,
        base=data["base"],
        isolation=data["isolation"],
        harness=data["harness"],
        firewall=data["firewall"],
        template=data["template"],
        setup=setup,
        ports=ports,
    )


def _parse_ports(worktree: Path, path: Path, value: Any) -> PortBlock | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"base", "count"}
        or type(value["base"]) is not int
        or type(value["count"]) is not int
        or value["base"] < 1
        or value["count"] < 1
    ):
        raise _invalid(
            worktree, path, "ports must be null or an object with integer base and count"
        )
    return PortBlock(value["base"], value["count"])


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
        "setup": rec.setup.to_json(),
        "ports": None if rec.ports is None else {"base": rec.ports.base, "count": rec.ports.count},
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
    """Delete the record for ``worktree`` and its setup log (the last step of
    a complete teardown). Missing is fine."""
    for path in (record_path(worktree), setup_log_path(worktree)):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
