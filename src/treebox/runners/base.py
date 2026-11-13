"""The Runner protocol.

Provisioning (worktree, submodules, .env) is identical for every runner; only
this run seam differs. A runner ensures dependencies are present (``setup``)
and launches the agent (``launch`` / ``entry_command``). Runner-specific
teardown behavior (containers, volumes) is owned by the runner itself —
options like the docker runner's volume removal arrive at construction, not
through this protocol.

The contract has two parts. Every runner may assume provisioning already
happened host-side and that the host filesystem is visible to the agent at
identical absolute paths. Docker does this by bind-mounting the worktree and
its git common dir 1:1; the host-owned record lives beside the worktree, and
the lockfile hash stats host files. Backends that can't present host paths
verbatim (SSH-remote, VMs, cloud sandboxes) do not fit this seam; they would
need a filesystem-transport seam that deliberately does not exist yet.

Sandboxed runners additionally own the security invariants: staged credential
*copies* only, never the live host login dirs; the sandbox-defining config
rendered outside the mount; the shared ``.git/hooks`` presented read-only;
egress lockdown (when enabled) established before any workspace-derived code
runs; only user-level treebox config ever read. The host runner is the
deliberate non-sandbox exception: it launches directly on the host with live
login dirs and normal host repo access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ..harnesses import Harness
from ..models import Worktree
from ..output import Reporter


class PreflightError(RuntimeError):
    """A runner dependency is missing or unusable.

    Subclasses RuntimeError so existing handlers keep working, but carries a
    stable machine code (for ``--json`` consumers to branch on) and a
    remediation hint (rendered under CLI errors and as a `doctor` advisory)."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "MISSING_DEPENDENCY",
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint


class FirewallError(RuntimeError):
    """The requested egress lockdown could not be established.

    The sandbox is not entry-ready: the CLI classifies this
    (``FIREWALL_FAILED``, with the remediation ``hint``) and never launches
    the agent or emits an ``entry_command`` after it."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


RunnerTeardownStatus = Literal["cleaned", "skipped", "failed"]


@dataclass(frozen=True)
class RunnerTeardownResult:
    """Observable result of a runner-specific teardown attempt.

    ``failed`` means a container/image removal was attempted and failed after
    every remaining cleanup step was still tried. ``remaining`` names each
    runner resource that is still present because a step failed or could not
    run (``"<resource> (<reason>)"``, the cleanup result in ``cleanup.py``):
    while it is non-empty the caller keeps the worktree and its ownership
    record, and the runner keeps the metadata a retry needs, so the same
    teardown can finish the job. Also reports whether per-runner volumes were
    actually removed.
    """

    container: RunnerTeardownStatus
    volumes_removed: bool = False
    remaining: tuple[str, ...] = ()

    @classmethod
    def cleaned(cls, *, volumes_removed: bool = False) -> RunnerTeardownResult:
        return cls("cleaned", volumes_removed=volumes_removed)

    @classmethod
    def skipped(cls) -> RunnerTeardownResult:
        return cls("skipped")


@dataclass(frozen=True)
class RunnerFacts:
    """Doctor-facing facts about a runner.

    Kept out of the operational methods so presentation vocabulary never
    leaks into the run seam: the checklist detail shown when ``preflight``
    passes, and where the agent runs. ``sandboxed`` decides what ``doctor``
    checks for the selected harness: on the host (False) the harness binary
    on ``PATH`` and the host login; in a sandbox (True) the login file the
    sandbox gets a copy of (the binary is part of the sandbox image and is
    checked at launch).
    """

    preflight_detail: str
    sandboxed: bool


@runtime_checkable
class Runner(Protocol):
    name: str

    def preflight(self, reporter: Reporter) -> None:
        """Verify this runner's host dependencies before provisioning (also
        the `doctor` runner check). Raises PreflightError on failure."""
        ...

    def facts(self) -> RunnerFacts:
        """Doctor-facing facts about this runner (see ``RunnerFacts``)."""
        ...

    def initialize(self, wt: Worktree) -> None:
        """Persist runner-owned host metadata before this worktree's first
        setup. Sandboxed runners keep security-sensitive ownership records
        outside every sandbox-writable mount; host runners may no-op."""
        ...

    def setup(self, wt: Worktree, *, cold: bool, reporter: Reporter) -> None:
        """Ensure dependencies are installed (cache-backed unless ``cold``)."""
        ...

    def refresh(self, wt: Worktree, *, reporter: Reporter) -> None:
        """Re-stage state that must never go stale between sessions (the docker
        runner's credential copies). Runs on EVERY ``enter``, independent of the
        lockfile-hash skip that gates ``setup`` — auth is not a cache."""
        ...

    def dry_run_setup(
        self,
        wt: Worktree,
        *,
        cold: bool,
        source_ref: str | None,
    ) -> list[str]:
        """The setup commands this runner *would* run, for ``--dry-run``.

        ``wt`` may not exist on disk yet (``create --dry-run``); runners that
        need repository context read ``wt.repo``. ``source_ref`` is the Git
        revision a new worktree will use; ``None`` means a resumed worktree at
        ``wt.path``. ``cold`` must select the same cache isolation as ``setup``
        without creating its disposable cache."""
        ...

    def prepare_entry(self, wt: Worktree) -> None:
        """Prepare the sandbox before launch or entry-command emission.

        The CLI calls this method on every entry, before ``launch`` or
        ``--print``/``--json`` output. Docker starts a stopped container and
        checks its enabled firewall even when the container is already running.
        A failed firewall initialization raises ``FirewallError`` and blocks
        launch and command emission. This method is a no-op on the host."""
        ...

    def entry_command(self, wt: Worktree, *, harness: Harness, args: list[str]) -> list[str]:
        """The argv that launches the agent (for --print)."""
        ...

    def launch(self, wt: Worktree, *, harness: Harness, args: list[str]) -> int:
        """Launch the agent, returning its exit code."""
        ...

    def teardown(self, wt: Worktree, *, reporter: Reporter) -> RunnerTeardownResult:
        """Tear down this runner's resources for ``wt``, honoring any
        teardown options the runner was constructed with. Warn and keep going
        when a cleanup step fails, then report what actually happened - and
        what remains - in the result instead of raising. Runner metadata a
        retry needs is kept while anything remains.
        """
        ...
