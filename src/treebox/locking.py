"""Cross-process advisory locking: one lock per worktree, one machine-wide
lock for the port-block ledger.

- **A worktree** (``worktree_lock``). ``create``, ``enter``, and ``teardown``
  of the same worktree must not race on its directory, its container, or the
  shared caches it writes through. The lock is keyed by the worktree's *path*
  - the identity every command agrees on - so callers pass the directory they
  are about to touch and never need to know where the lock file lives. Two
  worktrees with the same leaf name under different parents (a nested root)
  therefore take different locks. It is non-blocking: a second caller for the
  same worktree fails fast (``LockError``) instead of corrupting a half-built
  tree or deleting one that is in use.

- **The port-block ledger** (``ports_lock``). Port blocks are unique across
  every repo and root on the machine, so their lock is one level above any
  repository: a file in the user-level treebox home (``ports.py`` owns where).
  It is *waiting*: creates of different worktrees are legitimate in parallel,
  so they queue for the short ledger read-modify-write. ``LockError`` only
  when the wait passes ``timeout``, which means another process is stuck.

Lock order: the worktree lock first, then the ports lock, never the other way
round; the ports lock is never nested and is held only for the ledger update.
While holding it, the allocator may *probe* other worktrees' locks
(``worktree_lock_held``), which never waits, so it cannot deadlock.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# How long a caller queues for the ports ledger before giving up. The section
# is a small JSON read-modify-write (normally milliseconds); the limit only
# turns a stuck holder into a classified failure instead of a hang.
PORTS_LOCK_TIMEOUT = 30.0
_POLL_INTERVAL = 0.05
_BUSY = (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK)


class LockError(RuntimeError):
    """A lock is held by another process. ``hint`` is the recovery advice."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def _worktree_lock_path(path: Path) -> Path:
    return path.parent / ".locks" / f"{path.name}.lock"


@contextmanager
def worktree_lock(path: Path) -> Iterator[None]:
    """Hold the exclusive lock for the worktree at ``path`` for the block.

    ``path`` need not exist yet (``create`` locks before ``git worktree add``).
    The lock file lives beside the worktree, in ``<parent>/.locks/<leaf>.lock``,
    outside every sandbox mount.
    """
    name = path.name
    try:
        import fcntl
    except ImportError:  # non-POSIX: locking unsupported, proceed unguarded
        yield
        return

    lock_path = _worktree_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Deliberately not a `with`: the descriptor must stay open (holding the
    # flock) for the whole yielded block; the finally below closes it.
    fd = open(lock_path, "w")  # noqa: SIM115
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _BUSY:
                raise LockError(
                    f"Another treebox is already working on '{name}'. "
                    "Wait for it to finish or use a different worktree."
                ) from exc
            raise
        yield
    finally:
        fd.close()


def worktree_lock_held(path: Path) -> bool:
    """Whether some process holds the worktree lock for ``path`` right now.

    Never waits and never creates anything (the allocator calls it for other
    repositories' worktrees). A lock file that does not exist is not held. A
    probe from the process that holds the lock also reports it held: flock
    locks belong to the open file, not to the process."""
    try:
        import fcntl
    except ImportError:
        return False
    try:
        fd = os.open(_worktree_lock_path(path), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _BUSY:
            return True
        raise
    finally:
        os.close(fd)  # also releases the probe's own lock
    return False


@contextmanager
def ports_lock(lock_path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Hold the machine-wide port-ledger lock at ``lock_path`` for the block,
    waiting for other treebox processes (``PORTS_LOCK_TIMEOUT`` by default).
    Keep the block to the ledger read-modify-write."""
    try:
        import fcntl
    except ImportError:  # non-POSIX: locking unsupported, proceed unguarded
        yield
        return

    limit = PORTS_LOCK_TIMEOUT if timeout is None else timeout
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Not a `with`: the descriptor must stay open (holding the flock) for the
    # whole yielded block; the finally below closes it.
    fd = open(lock_path, "w")  # noqa: SIM115
    try:
        started = time.monotonic()
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in _BUSY:
                    raise
            if time.monotonic() - started >= limit:
                raise LockError(
                    f"Another treebox process has held the port-block ledger lock "
                    f"({lock_path}) for over {limit:.0f}s.",
                    hint="Wait for it to finish, then retry. If no treebox is running, "
                    "a process is stuck holding the lock: find it with "
                    f"`lsof {lock_path}`.",
                )
            time.sleep(_POLL_INTERVAL)
        yield
    finally:
        fd.close()
