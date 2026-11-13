"""Cross-process advisory locking: one lock per worktree, one per repository,
and one machine-wide lock for the port-block ledger.

Three different things need exclusion, on three different keys:

- **A worktree** (``worktree_lock``). ``create``, ``enter``, and ``teardown``
  of the same worktree must not race on its directory, its container, or the
  shared caches it writes through. The lock is keyed by the worktree's *path*
  - the identity every command agrees on - so callers pass the directory they
  are about to touch and never need to know where the lock file lives. Two
  worktrees with the same leaf name under different parents (a nested root)
  therefore take different locks. It is non-blocking: a second caller for the
  same worktree fails fast (``LockError``) instead of corrupting a half-built
  tree or deleting one that is in use.

- **The repository's shared git state** (``repo_lock``). Worktrees with
  *different* names still share one git common dir, and several steps write
  to it: ``git fetch`` (refs, ``FETCH_HEAD``), ``git worktree add`` /
  ``remove`` / ``prune`` (``.git/worktrees/``, branch refs, and the upstream
  section it writes to ``.git/config``), and treebox's own edits to the shared
  config and ``info/exclude``. Git guards its config with a try-once lock (no
  waiting) and creates a worktree's admin dir in several non-atomic steps, so
  two unserialized writers fail each other at random. This lock is *waiting*:
  parallel creates of different names are legitimate, so they queue for the
  short git section and then run their slow setup concurrently.

- **The port-block ledger** (``ports_lock``). Port blocks are unique across
  every repo and root on the machine, so their lock is one level above any
  repository: a file in the user-level treebox home (``ports.py`` owns where).
  It is *waiting* too, held only for the short ledger read-modify-write.

Lock order: the worktree lock first, then either the repo lock or the ports
lock, never the other way round. The repo lock and the ports lock are never
nested: not in each other, and not in themselves. While holding the ports
lock, the allocator may *probe* other worktrees' locks
(``worktree_lock_held``), which never waits, so it cannot deadlock.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import git

# The lock file's name inside the git common dir. Keying by the common dir (not
# by the worktree root) is what makes the lock cover every root and every
# linked worktree of one repository.
_REPO_LOCK_FILE = "treebox-repo.lock"
# How long a caller queues for the repository before giving up. Sized for a
# slow ``git fetch`` ahead of it in the queue, not for the common case (the
# section is normally well under a second).
REPO_LOCK_TIMEOUT = 300.0
# How long a caller queues for the ports ledger before giving up. The section
# is a small JSON read-modify-write (normally milliseconds); the limit only
# turns a stuck holder into a classified failure instead of a hang.
PORTS_LOCK_TIMEOUT = 30.0
_POLL_INTERVAL = 0.05
# Say something once a wait has been noticeable; never for an uncontended lock.
_ANNOUNCE_AFTER = 1.0
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
def _waiting_lock(
    lock_path: Path,
    *,
    limit: float,
    stuck: Callable[[], LockError],
    on_wait: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Hold the flock on ``lock_path`` for the block, polling until it is
    free. ``on_wait`` is called once if the wait becomes noticeable; after
    ``limit`` seconds the error ``stuck()`` is raised."""
    try:
        import fcntl
    except ImportError:  # non-POSIX: locking unsupported, proceed unguarded
        yield
        return

    # Not a `with`: the descriptor must stay open (holding the flock) for the
    # whole yielded block; the finally below closes it.
    fd = open(lock_path, "w")  # noqa: SIM115
    try:
        started = time.monotonic()
        announced = False
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in _BUSY:
                    raise
            waited = time.monotonic() - started
            if waited >= limit:
                raise stuck()
            if on_wait is not None and not announced and waited >= _ANNOUNCE_AFTER:
                announced = True
                on_wait()
            time.sleep(_POLL_INTERVAL)
        yield
    finally:
        fd.close()


@contextmanager
def repo_lock(
    path: str | Path,
    *,
    timeout: float | None = None,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Hold the exclusive lock on the shared git state of the repository that
    contains ``path`` (the main checkout or any linked worktree) for the block.

    Waits for other treebox processes instead of failing: queueing is the
    point. ``on_wait`` is called once if the wait becomes noticeable, so a
    caller can tell the user why it is paused. Raises ``LockError`` only when
    ``timeout`` seconds pass without the lock, which means another operation
    is stuck rather than busy (``REPO_LOCK_TIMEOUT`` by default).

    Keep the block to git operations: never hold it across dependency setup,
    a container build, the agent session, or the ports lock.
    """
    limit = REPO_LOCK_TIMEOUT if timeout is None else timeout

    def stuck() -> LockError:
        return LockError(
            "Another treebox operation has held this repository's git "
            f"state for over {limit:.0f}s. Wait for it to finish, then retry."
        )

    with _waiting_lock(
        git.common_dir(path) / _REPO_LOCK_FILE, limit=limit, stuck=stuck, on_wait=on_wait
    ):
        yield


@contextmanager
def ports_lock(lock_path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Hold the machine-wide port-ledger lock at ``lock_path`` for the block,
    waiting for other treebox processes (``PORTS_LOCK_TIMEOUT`` by default).
    Keep the block to the ledger read-modify-write."""
    limit = PORTS_LOCK_TIMEOUT if timeout is None else timeout

    def stuck() -> LockError:
        return LockError(
            f"Another treebox process has held the port-block ledger lock "
            f"({lock_path}) for over {limit:.0f}s.",
            hint="Wait for it to finish, then retry. If no treebox is running, "
            "a process is stuck holding the lock: find it with "
            f"`lsof {lock_path}`.",
        )

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _waiting_lock(lock_path, limit=limit, stuck=stuck):
        yield
