"""Cross-process advisory locking: one lock per worktree, one per repository.

Two different things need exclusion, on two different keys:

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

Lock order is always worktree lock, then repo lock; nothing takes them the
other way round, and ``repo_lock`` is never nested.
"""

from __future__ import annotations

import errno
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
_POLL_INTERVAL = 0.05
# Say something once a wait has been noticeable; never for an uncontended lock.
_ANNOUNCE_AFTER = 1.0


class LockError(RuntimeError):
    pass


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

    lock_dir = path.parent / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    # Deliberately not a `with`: the descriptor must stay open (holding the
    # flock) for the whole yielded block; the finally below closes it.
    fd = open(lock_path, "w")  # noqa: SIM115
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockError(
                    f"Another treebox is already working on '{name}'. "
                    "Wait for it to finish or use a different worktree."
                ) from exc
            raise
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
    a container build, or the agent session.
    """
    try:
        import fcntl
    except ImportError:  # non-POSIX: locking unsupported, proceed unguarded
        yield
        return

    limit = REPO_LOCK_TIMEOUT if timeout is None else timeout
    lock_path = git.common_dir(path) / _REPO_LOCK_FILE
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
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
            waited = time.monotonic() - started
            if waited >= limit:
                raise LockError(
                    "Another treebox operation has held this repository's git "
                    f"state for over {limit:.0f}s. Wait for it to finish, then retry."
                )
            if on_wait is not None and not announced and waited >= _ANNOUNCE_AFTER:
                announced = True
                on_wait()
            time.sleep(_POLL_INTERVAL)
        yield
    finally:
        fd.close()
