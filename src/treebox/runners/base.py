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
its git common dir 1:1; state lives in the host-side private git dir, and the
lockfile hash stats host files. Backends that can't present host paths
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


RunnerTeardownStatus = Literal["cleaned", "skipped", "failed"]


@dataclass(frozen=True)
class RunnerTeardownResult:
    """Observable result of a runner-specific teardown attempt.

    ``failed`` means a container/image removal was attempted and failed after
    every remaining cleanup step was still tried - the caller deletes the
    worktree's recorded state right after teardown, so a step skipped here
    could never be retried and its resources would leak forever. This result
    distinguishes that from an honest skip, and reports whether per-runner
    volumes were actually removed.
    """

    container: RunnerTeardownStatus
    volumes_removed: bool = False

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
    passes, and whether a missing subscription login is a hard gate in
    ``doctor``'s machine verdict for this runner.
    """

    preflight_detail: str
    login_required: bool


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

    def setup(self, wt: Worktree, *, cold: bool, reporter: Reporter) -> None:
        """Ensure dependencies are installed (cache-backed unless ``cold``)."""
        ...

    def workspace_volumes(self, wt: Worktree) -> list[str] | None:
        """The per-workspace volume names this runner's setup creates for
        ``wt``, recorded in the worktree state at create time so teardown can
        still remove them when the container and the template are both gone.
        None means the runner has no per-workspace volumes concept."""
        ...

    def refresh(self, wt: Worktree, *, reporter: Reporter) -> None:
        """Re-stage state that must never go stale between sessions (the docker
        runner's credential copies). Runs on EVERY ``enter``, independent of the
        lockfile-hash skip that gates ``setup`` — auth is not a cache."""
        ...

    def dry_run_setup(self, wt: Worktree) -> list[str]:
        """The setup commands this runner *would* run, for ``--dry-run``.

        ``wt`` may not exist on disk yet (``create --dry-run``); runners that
        need repository context read ``wt.repo``."""
        ...

    def prepare_entry(self, wt: Worktree) -> None:
        """Make the sandbox entry-ready before the agent (or an emitted
        ``entry_command``) enters it: the docker runner restarts a stopped
        container and re-establishes the firewall; a no-op on the host. The
        CLI calls it before emitting ``--print``/``--json`` output, and
        ``launch`` implementations needing the same guarantee call it
        themselves (the docker runner's does) - so an emitted
        ``entry_command`` is never dead on arrival and a treebox-owned
        restart never leaves egress silently open."""
        ...

    def entry_command(self, wt: Worktree, *, harness: Harness, args: list[str]) -> list[str]:
        """The argv that launches the agent (for --print)."""
        ...

    def launch(self, wt: Worktree, *, harness: Harness, args: list[str]) -> int:
        """Launch the agent, returning its exit code."""
        ...

    def teardown(self, wt: Worktree, *, reporter: Reporter) -> RunnerTeardownResult:
        """Tear down this runner's resources for ``wt``, honoring any
        teardown options the runner was constructed with. Best-effort: warn
        and keep going when a cleanup step fails, then report what actually
        happened in the result instead of raising.
        """
        ...
