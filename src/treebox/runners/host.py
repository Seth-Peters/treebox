"""Host runner: run setup and the agent directly on the host, in the worktree.

This is the default and the SSH-first path — no container, no docker. Setup
commands run in the host shell with the shared package caches wired in; the
agent launches in the worktree directory using the host's subscription login.
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from typing import TYPE_CHECKING

from .. import ecosystems
from ..harnesses import Harness
from ..models import Worktree
from ..output import Reporter
from ..resources import ResourceLimitError, ResourceLimits
from ..system import SessionCancelled, have, run_agent, signal_name
from .base import PreflightError, RunnerFacts, RunnerTeardownResult

if TYPE_CHECKING:
    from ..config import Config

_FACTS = RunnerFacts(preflight_detail="no container dependencies", sandboxed=False)
_DRY_RUN_COLD_ROOT = "<temporary-dir>/treebox-cold-XXXXXXXX"


def _render_cache_env(command: str, env: dict[str, str]) -> str:
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return f"{assignments} {command}" if assignments else command


class HostRunner:
    name = "host"
    supported_resources: frozenset[str] = frozenset()

    def __init__(self, config: Config) -> None:
        self.config = config

    def preflight(self, reporter: Reporter) -> None:
        return

    def facts(self) -> RunnerFacts:
        return _FACTS

    def template_resources(self) -> ResourceLimits:
        return ResourceLimits()

    def check_resources(self, limits: ResourceLimits) -> None:
        if limits.names():
            raise ResourceLimitError(
                limits.names()[0], "The host runner cannot apply resource limits."
            )

    def initialize(self, wt: Worktree) -> None:
        return

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

    def setup_reason(self, wt: Worktree) -> str | None:
        return None

    def refresh(self, wt: Worktree, *, reporter: Reporter) -> None:
        return

    def _run_auto(self, wt: Worktree, cold_root: str | None, reporter: Reporter) -> None:
        ecos = ecosystems.detect(wt.path)
        if not ecos:
            reporter.note("setup", "no package manifests")
            return
        steps = ecosystems.setup_steps(ecos, self.config.caches, cold_cache_root=cold_root)
        ecosystems.ensure_cache_dirs(step.cache_dir for step in steps)
        for step in steps:
            if not have(step.argv[0]):
                reporter.note(f"setup · {step.name}", f"{step.argv[0]} not found; skipped")
                continue
            env = dict(os.environ)
            env.update(step.env)
            env.update(wt.treebox_env())
            detail = "from-source (cold)" if cold_root else "cache-backed"
            reporter.step(f"setup · {step.name}", detail, step.argv, cwd=str(wt.path), env=env)

    def _run_override(self, wt: Worktree, cold_root: str | None, reporter: Reporter) -> None:
        cache_env = ecosystems.cache_env(self.config.caches, cold_cache_root=cold_root)
        ecosystems.ensure_cache_dirs(cache_env.values())
        env = dict(os.environ)
        env.update(cache_env)
        env.update(wt.treebox_env())
        for i, command in enumerate(self.config.setup_hook or []):
            reporter.step(
                f"setup · hook {i + 1}", "done", ["sh", "-c", command], cwd=str(wt.path), env=env
            )

    def dry_run_setup(
        self,
        wt: Worktree,
        *,
        cold: bool,
        source_ref: str | None,
    ) -> list[str]:
        if self.config.setup_hook is not None:
            commands = [f"sh -c {shlex.quote(c)}" for c in self.config.setup_hook]
            if not cold:
                return commands
            env = ecosystems.cache_env(self.config.caches, cold_cache_root=_DRY_RUN_COLD_ROOT)
            return [_render_cache_env(command, env) for command in commands]
        if source_ref is not None:
            ecos = ecosystems.detect_revision(wt.repo, source_ref)
        else:
            source = wt.path if wt.path.exists() else wt.repo
            ecos = ecosystems.detect(source)
        if not ecos:
            return ["# no package manifests — setup is a no-op"]
        steps = ecosystems.setup_steps(
            ecos,
            self.config.caches,
            cold_cache_root=_DRY_RUN_COLD_ROOT if cold else None,
        )
        if not cold:
            return [" ".join(step.argv) for step in steps]
        return [_render_cache_env(shlex.join(step.argv), step.env) for step in steps]

    def prepare_entry(self, wt: Worktree) -> None:
        return

    def entry_command(self, wt: Worktree, *, harness: Harness, args: list[str]) -> list[str]:
        argv = harness.launch_argv(args)
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in wt.treebox_env().items())
        return [
            "sh",
            "-c",
            f"cd {shlex.quote(str(wt.path))} && export {exports} && exec {shlex.join(argv)}",
        ]

    def launch(self, wt: Worktree, *, harness: Harness, args: list[str]) -> int:
        cmd = harness.launch_argv(args)
        if not have(cmd[0]):
            raise PreflightError(
                f"'{cmd[0]}' not found on PATH.",
                error_code="HARNESS_MISSING",
                hint=harness.login_hint(),
            )
        code, cancelled = run_agent(cmd, cwd=str(wt.path), env={**os.environ, **wt.treebox_env()})
        if cancelled is not None:
            name = signal_name(cancelled)
            raise SessionCancelled(
                cancelled, f"Cancelled by {name}: the agent was sent {name} and has exited."
            )
        return code

    def teardown(self, wt: Worktree, *, reporter: Reporter) -> RunnerTeardownResult:
        reporter.note("container", "skipped · host isolation")
        return RunnerTeardownResult.skipped()
