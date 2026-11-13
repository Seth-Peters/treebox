"""The machine-wide port-block allocator.

Each worktree gets one block of consecutive port numbers (``TREEBOX_PORT_BASE``
and ``TREEBOX_PORT_COUNT``) for its repo-owned services. Blocks must not
overlap across *every* repo and worktree root on the machine, so the
allocator's state lives one level above any repository, in the user-level
treebox home (``$TREEBOX_HOME``, default ``~/.treebox``)::

    $TREEBOX_HOME/ports.json   the ledger: which blocks are taken, by whom
    $TREEBOX_HOME/ports.lock   its lock (locking.ports_lock)

**One source of truth.** The ledger alone says which blocks are taken. Each
entry names its owner by registered worktree path (and the repo, to check the
registration). The host-owned record (``record.py``) keeps a copy of its own
block so ``list`` and the entry commands can read it without the lock, but a
record never makes a block taken: every allocation decision reads only the
ledger, under the lock, and writes it atomically before the lock is released.

**Order of writes** (each command already holds the worktree lock):

- ``create`` / ``enter``: ledger entry first (``reserve``), then the record
  copy. A crash between the two leaves an entry without a record copy; the
  next ``reserve`` for the same path returns that entry, so nothing leaks
  and nothing changes.
- ``teardown``: the record goes when cleanup is complete, then the entry
  (``release``). A crash between the two leaves an entry whose worktree is
  gone; it is stale and the next allocation reclaims it.

**Stale entries.** An entry is reclaimed only when nothing can still use its
block: its directory does not exist, git has no registration for it (so
neither ``enter`` nor ``teardown`` can reach it), and no process holds its
worktree lock (so no ``create`` is between its ledger write and its
``git worktree add``). A worktree that a failed teardown kept (directory or
registration still present) keeps its block for the retry.

What this does not do: it keeps treebox's own allocations apart. It does not
reserve a socket - another program on the host can still bind a port in a
block - and it starts no service.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import git, locking
from .config import PortRange, treebox_home
from .models import PortBlock, same_path

LEDGER_FILE = "ports.json"
LOCK_FILE = "ports.lock"
LEDGER_SCHEMA_VERSION = 1
_ENTRY_KEYS = frozenset({"base", "count", "worktree", "repo"})


class PortsError(RuntimeError):
    """A port-block failure the CLI classifies; ``hint`` and ``path`` are
    surfaced as ``error.hint`` / ``error.path``."""

    hint: str | None = None
    path: Path | None = None


class PortsExhaustedError(PortsError):
    """No free block is left in the configured range (exit 5, PORTS_EXHAUSTED)."""

    def __init__(self, ports: PortRange, taken: int) -> None:
        super().__init__(
            f"No free port block of {ports.block_size} is left in {ports.start}-{ports.end}: "
            f"{taken} block(s) are reserved by other worktrees."
        )
        self.path = ledger_path()
        self.hint = (
            "Tear down worktrees you no longer need (treebox teardown <name>), or widen "
            "[ports] start / end (or lower block_size) in the user config."
        )


class LedgerInvalidError(PortsError):
    """The ledger cannot be read or trusted (exit 1, PORT_LEDGER_INVALID).
    It is never guessed at or rewritten: that could hand out a taken block."""

    def __init__(self, reason: str) -> None:
        path = ledger_path()
        super().__init__(f"The port-block ledger {path} is invalid: {reason}.")
        self.path = path
        self.hint = (
            "Fix or move the file aside. Without it treebox forgets which blocks are "
            "taken; each worktree takes its recorded block back on its next enter."
        )


@dataclass(frozen=True)
class _Entry:
    block: PortBlock
    worktree: str
    repo: str


def ledger_path() -> Path:
    return treebox_home() / LEDGER_FILE


def reserve(
    worktree: Path, repo: str, ports: PortRange, *, preferred: PortBlock | None = None
) -> tuple[PortBlock, bool]:
    """The block of ``worktree``, reserving one when it has none. Returns the
    block and whether this call reserved it (False: the ledger already had it).

    ``preferred`` is the block the worktree's record holds: taken back when
    the ledger lost its entry and the block is still free, so the values a
    worktree's services use stay stable. Raises ``PortsExhaustedError`` when
    the range has no free block, before anything else is written."""
    with locking.ports_lock(treebox_home() / LOCK_FILE):
        entries = _load()
        own = _own(entries, worktree)
        if own is not None:
            return own.block, False
        live = [entry for entry in entries if not _stale(entry)]
        taken = [entry.block for entry in live]
        block = (
            preferred
            if preferred is not None and not any(preferred.overlaps(t) for t in taken)
            else _first_free(ports, taken)
        )
        if block is None:
            raise PortsExhaustedError(ports, len(taken))
        _save([*live, _Entry(block, str(worktree), repo)])
        return block, True


def release(worktree: Path) -> PortBlock | None:
    """Free the block of ``worktree``; the block, or None when it had none.
    Called once cleanup is complete (the record is already gone)."""
    if not ledger_path().exists():
        return None
    with locking.ports_lock(treebox_home() / LOCK_FILE):
        entries = _load()
        own = _own(entries, worktree)
        if own is None:
            return None
        _save([entry for entry in entries if entry is not own])
        return own.block


def preview(worktree: Path, ports: PortRange, *, preferred: PortBlock | None = None) -> PortBlock:
    """The block ``reserve`` would return now, for ``--dry-run``: read-only,
    without the lock, and it reserves nothing. Raises like ``reserve``."""
    entries = _load()
    own = _own(entries, worktree)
    if own is not None:
        return own.block
    taken = [entry.block for entry in entries if not _stale(entry)]
    if preferred is not None and not any(preferred.overlaps(t) for t in taken):
        return preferred
    block = _first_free(ports, taken)
    if block is None:
        raise PortsExhaustedError(ports, len(taken))
    return block


def _own(entries: list[_Entry], worktree: Path) -> _Entry | None:
    # Matched by the resolved path: a root spelled through an alias (/var vs
    # /private/var) is still the same worktree and keeps its block.
    return next((e for e in entries if same_path(e.worktree, worktree)), None)


def _first_free(ports: PortRange, taken: list[PortBlock]) -> PortBlock | None:
    """The lowest block of the range that overlaps no taken block. Taken
    blocks may come from another range or block size (the config changed),
    so the check is overlap, not equal bases."""
    base = ports.start
    while base + ports.block_size - 1 <= ports.end:
        candidate = PortBlock(base, ports.block_size)
        if not any(candidate.overlaps(t) for t in taken):
            return candidate
        base += ports.block_size
    return None


def _stale(entry: _Entry) -> bool:
    """Whether nothing can still use ``entry``'s block (see the module doc).
    Read-only, and conservative: any doubt keeps the block."""
    path = Path(entry.worktree)
    if os.path.lexists(path) or locking.worktree_lock_held(path):
        return False
    if not Path(entry.repo).is_dir():
        return True  # the repo is gone, and with it every registration
    try:
        return not git.worktree_registered(entry.repo, entry.worktree)
    except git.GitError:
        return False


def _load() -> list[_Entry]:
    path = ledger_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LedgerInvalidError(f"cannot read it: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LedgerInvalidError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"schemaVersion", "blocks"}:
        raise LedgerInvalidError("expected an object with schemaVersion and blocks")
    if data["schemaVersion"] != LEDGER_SCHEMA_VERSION:
        raise LedgerInvalidError(f"unsupported schemaVersion {data['schemaVersion']!r}")
    if not isinstance(data["blocks"], list):
        raise LedgerInvalidError("blocks must be a list")
    entries = [_parse_entry(item) for item in data["blocks"]]
    for i, entry in enumerate(entries):
        for other in entries[i + 1 :]:
            if entry.block.overlaps(other.block) or entry.worktree == other.worktree:
                raise LedgerInvalidError(
                    f"entries for {entry.worktree} and {other.worktree} conflict"
                )
    return entries


def _parse_entry(item: Any) -> _Entry:
    if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
        raise LedgerInvalidError(f"each block needs exactly {', '.join(sorted(_ENTRY_KEYS))}")
    base, count, worktree, repo = item["base"], item["count"], item["worktree"], item["repo"]
    if type(base) is not int or type(count) is not int or count < 1 or base < 1:
        raise LedgerInvalidError(f"block {item!r} needs a positive integer base and count")
    if not isinstance(worktree, str) or not isinstance(repo, str):
        raise LedgerInvalidError(f"block {item!r} needs string worktree and repo paths")
    return _Entry(PortBlock(base, count), worktree, repo)


def _save(entries: list[_Entry]) -> None:
    """Write the ledger atomically (complete temp file, fsync, rename), so a
    crash leaves the old ledger or the new one, never a partial file."""
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": LEDGER_SCHEMA_VERSION,
        "blocks": [
            {
                "base": e.block.base,
                "count": e.block.count,
                "worktree": e.worktree,
                "repo": e.repo,
            }
            for e in sorted(entries, key=lambda e: e.block.base)
        ],
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
