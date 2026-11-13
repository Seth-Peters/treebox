"""Host-environment helpers for SSH-Linux ergonomics.

We resolve the invoking user's UID/GID so any bind mounts keep host ownership,
probe PATH for required tools, and run the agent in the foreground with
defined cancellation (``run_agent``).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from shutil import which
from typing import Any


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int


def identity() -> Identity:
    """Invoking user's UID/GID (for correct bind-mount ownership)."""
    try:
        return Identity(uid=os.getuid(), gid=os.getgid())
    except AttributeError:  # non-POSIX; mounts won't be used there anyway
        return Identity(uid=1000, gid=1000)


def have(cmd: str) -> bool:
    return which(cmd) is not None


# The signals that cancel a running agent session, in both runners.
CANCEL_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# How long the agent gets to exit after a forwarded cancel signal before it is
# killed (SIGKILL).
STOP_GRACE_SECONDS = 10.0


class SessionCancelled(RuntimeError):
    """A cancel signal (``CANCEL_SIGNALS``) reached treebox while the agent
    ran. The agent was stopped; treebox exits ``128 + signum``."""

    def __init__(self, signum: int, message: str) -> None:
        super().__init__(message)
        self.signum = signum


def signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def run_agent(
    argv: list[str],
    *,
    cwd: str | None = None,
    stop: Callable[[subprocess.Popen[bytes], int], None] | None = None,
    grace: float = STOP_GRACE_SECONDS,
) -> tuple[int, int | None]:
    """Run the agent in the foreground with defined cancellation.

    Each cancel signal treebox receives while the agent runs is passed on
    with ``stop(proc, signum)`` (default: the same signal to the child
    process). If the agent is still running ``grace`` seconds after the
    first one, ``stop(proc, SIGKILL)`` ends it. Returns ``(exit code, the
    first cancel signal or None)`` once the child process has exited; a
    negative code means the child was killed by that signal.
    """

    def default_stop(proc: subprocess.Popen[bytes], signum: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signum)

    forward = stop or default_stop
    received: list[int] = []
    timer: threading.Timer | None = None
    proc = subprocess.Popen(argv, cwd=cwd)

    def on_signal(signum: int, _frame: object) -> None:
        nonlocal timer
        received.append(signum)
        forward(proc, signum)
        if timer is None:
            timer = threading.Timer(grace, forward, (proc, signal.SIGKILL))
            timer.daemon = True
            timer.start()

    previous: dict[int, Any] = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in CANCEL_SIGNALS:
                previous[sig] = signal.signal(sig, on_signal)
        code = proc.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if timer is not None:
            timer.cancel()
        if proc.poll() is None:  # an unexpected error while waiting: never orphan it
            proc.kill()
            proc.wait()
    return code, received[0] if received else None
