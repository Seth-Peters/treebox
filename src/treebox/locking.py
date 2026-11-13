"""Cross-process advisory locking on one worktree directory.

``create``, ``enter``, and ``teardown`` of the same worktree must not race on
its directory, its container, or the shared caches it writes through. The lock
is keyed by the worktree's *path* - the identity every command agrees on - so
callers pass the directory they are about to touch and never need to know
where the lock file lives. Two worktrees with the same leaf name under
different parents (a nested root) therefore take different locks.

A non-blocking POSIX ``flock`` makes a second caller fail fast (``LockError``)
instead of corrupting a half-built tree or deleting one that is in use.
"""

from __future__ import annotations

import errno
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


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
