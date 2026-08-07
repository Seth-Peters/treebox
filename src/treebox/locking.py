"""Cross-process advisory locking on a worktree name.

Two ``create``/``enter`` calls for the same worktree must not race on its dir
or the shared caches it writes through. We take a POSIX ``flock`` on a small
lock file keyed by the worktree name (the permanent identity — branches are
mutable), held for the duration of provisioning. Non-blocking with a clear
error so a second caller fails fast instead of corrupting a half-built tree.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from .models import worktree_root


class LockError(RuntimeError):
    pass


class LockRootChangedError(RuntimeError):
    """The pinned root for a stray-directory lock is no longer safe."""


@contextmanager
def worktree_lock(repo: str, root: str, name: str) -> Iterator[None]:
    """Hold an exclusive lock for the worktree ``name`` while provisioning it."""
    try:
        import fcntl
    except ImportError:  # non-POSIX: locking unsupported, proceed unguarded
        yield
        return

    lock_dir = worktree_root(repo, root) / ".locks"
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
def worktree_lock_at(root: str, root_identity: tuple[int, int], name: str) -> Iterator[None]:
    """Lock ``name`` through a pinned physical root without following links.

    Stray-directory teardown calls this after confirmation. No path-derived
    write occurs until the opened root identity matches the recorded device and
    inode. The root and lock directory descriptors then pin every later write.
    """
    try:
        import fcntl
    except ImportError:  # non-POSIX: locking unsupported, proceed unguarded
        yield
        return

    root_fd = -1
    lock_dir_fd = -1
    lock_fd = -1
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(root, flags)
        root_stat = os.fstat(root_fd)
        if (root_stat.st_dev, root_stat.st_ino) != root_identity:
            raise LockRootChangedError("The configured worktree root changed before locking.")
        with suppress(FileExistsError):
            os.mkdir(".locks", dir_fd=root_fd)
        lock_dir_fd = os.open(".locks", flags, dir_fd=root_fd)
        lock_dir_stat = os.fstat(lock_dir_fd)
        if not stat.S_ISDIR(lock_dir_stat.st_mode):
            raise LockRootChangedError("The worktree lock directory is not safe.")
        lock_name = f"{name}.lock"
        try:
            existing_lock = os.stat(lock_name, dir_fd=lock_dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing_lock = None
        if existing_lock is not None and not _safe_lock_file(existing_lock):
            raise LockRootChangedError("The worktree lock file is not safe.")
        lock_fd = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW,
            0o666,
            dir_fd=lock_dir_fd,
        )
        if not _safe_lock_file(os.fstat(lock_fd)):
            raise LockRootChangedError("The worktree lock file is not safe.")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockError(
                    f"Another treebox is already working on '{name}'. "
                    "Wait for it to finish or use a different worktree."
                ) from exc
            raise
        yield
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
            raise LockRootChangedError(
                "The configured worktree root changed before locking."
            ) from exc
        raise
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if lock_dir_fd >= 0:
            os.close(lock_dir_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _safe_lock_file(value: os.stat_result) -> bool:
    return stat.S_ISREG(value.st_mode) and value.st_nlink == 1
