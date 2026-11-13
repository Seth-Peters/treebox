"""The setup model and its persisted results.

**Setup model.** One ``SetupResult`` per worktree, kept in the host-owned
record (``record.py``), outside every sandbox mount, so a sandboxed agent can
never mark its own setup done or its dependencies fresh::

    state     meaning                                  recovery
    pending   setup started and did not finish         enter / repeated create
              (interrupted, or no result recorded)     runs setup again
    failed    a setup step exited non-zero             enter / repeated create
              (step, exit status kept)                 runs setup again
    done      setup finished against lockfileHash      enter re-runs setup only
                                                       when the hash changed

    create:  (no record) -> pending -> done | failed
    enter:   pending | failed -> pending -> done | failed
             done (hash changed) -> pending -> done | failed
    adopt:   any -> pending   (an adopted worktree gets setup on next enter)

``list`` reports ``fresh`` / ``stale`` only from a ``done`` result: a matching
hash alone never makes a worktree fresh.

Worktree cleanup is implemented by ``cleanup.cleanup_one``. It owns the
ordered removal of runner resources, the worktree and the ownership record.

"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import DepsFreshness, SetupStatus

# --- setup model -------------------------------------------------------------

SETUP_STATUSES: tuple[SetupStatus, ...] = ("pending", "failed", "done")


@dataclass(frozen=True)
class SetupResult:
    """What the last setup attempt of a worktree did. ``lockfile_hash`` is set
    only when ``done``; ``step`` / ``exit_status`` only when ``failed``."""

    status: SetupStatus = "pending"
    lockfile_hash: str = ""
    step: str = ""
    exit_status: int | None = None

    @classmethod
    def done(cls, lockfile_hash: str) -> SetupResult:
        return cls("done", lockfile_hash=lockfile_hash)

    @classmethod
    def failed(cls, step: str, exit_status: int) -> SetupResult:
        return cls("failed", step=step, exit_status=exit_status)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lockfileHash": self.lockfile_hash,
            "step": self.step,
            "exitStatus": self.exit_status,
        }

    @classmethod
    def from_json(cls, data: Any) -> SetupResult:
        """Parse the record's ``setup`` object. Raises ``ValueError`` on any
        shape it does not know (the record is then invalid, never guessed)."""
        if not isinstance(data, dict) or set(data) != {
            "status",
            "lockfileHash",
            "step",
            "exitStatus",
        }:
            raise ValueError("setup must be an object with status, lockfileHash, step, exitStatus")
        status, lockfile_hash, step, exit_status = (
            data["status"],
            data["lockfileHash"],
            data["step"],
            data["exitStatus"],
        )
        if status not in SETUP_STATUSES:
            raise ValueError(f"setup.status {status!r} is not one of {', '.join(SETUP_STATUSES)}")
        if not isinstance(lockfile_hash, str) or not isinstance(step, str):
            raise ValueError("setup.lockfileHash and setup.step must be strings")
        if exit_status is not None and type(exit_status) is not int:
            raise ValueError("setup.exitStatus must be an integer or null")
        if status != "done" and lockfile_hash:
            raise ValueError("setup.lockfileHash is set only when setup is done")
        return cls(status, lockfile_hash=lockfile_hash, step=step, exit_status=exit_status)


def setup_reason(result: SetupResult, current_hash: str) -> str | None:
    """Why ``enter`` must run setup now, or None when it may skip it."""
    if result.status == "pending":
        return "setup never completed - finishing setup"
    if result.status == "failed":
        return f"last setup failed at {result.step} (exit {result.exit_status}) - retrying setup"
    if result.lockfile_hash != current_hash:
        return "dependencies changed since last setup - re-syncing"
    return None


def resumable(result: SetupResult) -> bool:
    """Whether a repeated ``create`` finishes this worktree's setup instead of
    reporting a name conflict."""
    return result.status != "done"


def freshness(result: SetupResult, current_hash: str) -> DepsFreshness:
    """``list``'s DEPS value: fresh or stale only after a finished setup."""
    if result.status != "done":
        return "unknown"
    return "fresh" if result.lockfile_hash == current_hash else "stale"


def setup_log_path(worktree: Path) -> Path:
    """Where the output of the last failed setup step of ``worktree`` is kept:
    beside the record, outside every sandbox mount."""
    return worktree.parent / ".logs" / f"{worktree.name}.setup.log"
