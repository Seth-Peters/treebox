"""Host runner: run setup and the agent directly on the host, in the worktree.

This is the default and the SSH-first path — no container, no docker. Setup
commands run in the host shell with the shared package caches wired in; the
agent launches in the worktree directory using the host's subscription login.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

from .. import ecosystems
from ..harnesses import Harness
from ..models import Worktree
from ..output import Reporter, StepError
from ..system import have
from .base import RunnerFacts, RunnerTeardownResult

if TYPE_CHECKING:
    from ..config import Config

# A subscription login is the host runner's only hard requirement: the agent
# runs directly with the host's ~/.claude / ~/.codex. Beyond git (checked
# globally) and the agent CLI (verified at launch) there is nothing
# container-specific to check.
_FACTS = RunnerFacts(preflight_detail="no container dependencies", login_required=True)


class HostRunner:
    name = "host"

    def __init__(self, config: Config) -> None:
        self.config = config

    def preflight(self, reporter: Reporter) -> None:
        # Nothing to verify: see _FACTS.
        return

    def facts(self) -> RunnerFacts:
        return _FACTS

    def setup(self, wt: Worktree, *, cold: bool, reporter: Reporter) -> None:
        cold_root = tempfile.mkdtemp(prefix="treebox-cold-") if cold else None
        try:
            if self.config.setup_hook is not None:
                self._run_override(wt, cold_root, reporter)
            else:
                self._run_auto(wt, cold_root, reporter)
        finally:
            if cold_root:
                shutil.rmtree(cold_root, ignore_errors=True)

    def refresh(self, wt: Worktree, *, reporter: Reporter) -> None:
        # Nothing staged per-worktree: the agent runs with the live host
        # ~/.claude / ~/.codex, so host logins/logouts apply immediately.
        return

    def workspace_volumes(self, wt: Worktree) -> list[str] | None:
        return None  # volumes are a container concept; nothing to record

    def _run_auto(self, wt: Worktree, cold_root: str | None, reporter: Reporter) -> None:
        ecos = ecosystems.detect(wt.path)
        if not ecos:
            reporter.note("setup", "no package manifests")
            return
        steps = ecosystems.setup_steps(ecos, self.config.caches, cold_cache_root=cold_root)
        for step in steps:
            if not have(step.argv[0]):
                reporter.note(f"setup · {step.name}", f"{step.argv[0]} not found; skipped")
                continue
            env = dict(os.environ)
            env.update(step.env)
            detail = "from-source (cold)" if cold_root else "cache-backed"
            try:
                reporter.step(f"setup · {step.name}", detail, step.argv, cwd=str(wt.path), env=env)
            except StepError:
                # Setup failures are non-fatal: the tree is still usable and the
                # operator can fix deps inside the agent session.
                reporter.warn(f"{step.name} setup failed; continuing")

    def _run_override(self, wt: Worktree, cold_root: str | None, reporter: Reporter) -> None:
        env = dict(os.environ)
        env.update(ecosystems.cache_env(self.config.caches, cold_cache_root=cold_root))
        for i, command in enumerate(self.config.setup_hook or []):
            try:
                reporter.step(
                    f"setup · hook {i + 1}",
                    "done",
                    ["sh", "-c", command],
                    cwd=str(wt.path),
                    env=env,
                )
            except StepError:
                reporter.warn(f"setup hook step {i + 1} failed; continuing")

    def dry_run_setup(self, wt: Worktree) -> list[str]:
        if self.config.setup_hook is not None:
            return [f"sh -c {shlex.quote(c)}" for c in self.config.setup_hook]
        # The worktree doesn't exist yet; detect from the source repo's manifests.
        ecos = ecosystems.detect(wt.repo)
        if not ecos:
            return ["# no package manifests — setup is a no-op"]
        steps = ecosystems.setup_steps(ecos, self.config.caches, cold_cache_root=None)
        return [" ".join(s.argv) for s in steps]

    def prepare_entry(self, wt: Worktree) -> None:
        # Nothing to make ready: the agent runs directly on the host, so an
        # emitted entry_command works whenever the worktree exists.
        return

    def entry_command(self, wt: Worktree, *, harness: Harness, args: list[str]) -> list[str]:
        # Self-contained, like the docker runner's `docker exec -w`: the command
        # --print/--json emit must carry the worktree directory, or pasting it
        # launches a full-autonomy agent in whatever dir the user happens to be
        # in — typically the main checkout treebox exists to keep agents out of.
        argv = harness.launch_argv(args)
        return ["sh", "-c", f"cd {shlex.quote(str(wt.path))} && exec {shlex.join(argv)}"]

    def launch(self, wt: Worktree, *, harness: Harness, args: list[str]) -> int:
        cmd = harness.launch_argv(args)
        if not have(cmd[0]):
            raise RuntimeError(f"'{cmd[0]}' not found on PATH. {harness.login_hint()}")
        proc = subprocess.run(cmd, cwd=str(wt.path))
        return proc.returncode

    def teardown(self, wt: Worktree, *, reporter: Reporter) -> RunnerTeardownResult:
        # Nothing host-side to tear down beyond the worktree itself; the note
        # keeps the teardown checklist honest about what was (not) touched.
        reporter.note("container", "n/a (host isolation)")
        return RunnerTeardownResult.cleaned()
