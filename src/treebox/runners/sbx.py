"""sbx runner: provision identically, but run the agent in a Docker Sandboxes
(``sbx``) microVM - its own kernel and private Docker daemon - instead of a
container sharing the Docker Desktop VM.

``sbx`` is a standalone CLI that owns the sandbox lifecycle, the agent image,
agent login (``/login`` inside the sandbox) and a global network policy
(``sbx policy``). This runner therefore stages no credentials, renders no
templates and builds no image: it creates one sandbox per worktree and attaches
to it.

Host-locality: sbx mounts every workspace at its identical absolute host path,
so the worktree's ``.git`` pointer resolves unchanged inside the VM and this
runner fits the existing seam - no filesystem transport is needed.

Shared-``.git`` invariant: identical to the docker runner. The git common dir
is mounted writable (so in-sandbox git can commit) but ``hooks/`` is nested
read-only inside it, so a boxed agent cannot plant a hook that the operator's
next host-side git would execute. Residual, by design: the agent can rewrite
refs and git config, so treat the repo's ``.git`` as attacker-influenced after
a sandboxed session (see docker.py for the full discussion).

Ownership invariant: the sandbox name is recorded in a manifest beside the
worktree (outside every sandbox mount). Teardown removes only a sandbox that
manifest names - and whose primary workspace is still this worktree - never
anything derived from sandbox-writable state.

No host dependency install: macOS builds would not run in the Linux guest, so
``setup`` only creates the sandbox; dependencies are installed inside it.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import git, system
from ..harnesses import Harness
from ..models import Worktree
from ..output import Reporter
from .base import PreflightError, RunnerFacts, RunnerTeardownResult
from .docker import _CONFIG_ROOT

if TYPE_CHECKING:
    from ..config import Config

_MANIFEST = "treebox-sbx.json"
_MANIFEST_SCHEMA_VERSION = 1

# sbx owns agent login (inside the sandbox), so a missing host login is
# advisory. Whether sbx itself is signed in is not checkable non-interactively.
_FACTS = RunnerFacts(preflight_detail="sbx ok (login state not checkable)", login_required=False)

_INSTALL_HINT = "Install Docker Sandboxes (`brew install docker/tap/sbx`), then run `sbx login`."


def _sanitize(name: str) -> str:
    """An sbx-safe name fragment: lowercase ``[a-z0-9-]`` (sbx allows letters,
    digits, hyphens and periods, starting alphanumeric)."""
    safe = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return safe or "worktree"


def _sbx(args: list[str]) -> subprocess.CompletedProcess[str]:
    """The real sbx CLI seam (mirrors docker.py's ``_docker``). Never raises on
    a non-zero exit."""
    return subprocess.run(["sbx", *args], capture_output=True, text=True)


class SbxRunner:
    name = "sbx"

    def __init__(
        self,
        config: Config,
        *,
        sbx: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        """``sbx`` injects a canned CLI for tests; internal, not API."""
        self.config = config
        self._sbx_cli = sbx

    def _cli(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return (self._sbx_cli or _sbx)(args)

    # --- identity & ownership ----------------------------------------------------

    def _slug(self, wt: Worktree) -> str:
        """Deterministic sandbox name. The path hash keeps it unique across
        repos whose worktrees share a name and valid for names with ``/``."""
        digest = hashlib.sha256(str(wt.path).encode()).hexdigest()[:10]
        return f"treebox-{_sanitize(wt.name)[:40]}-{digest}"

    def _manifest_file(self, wt: Worktree) -> Path:
        return wt.path.parent / _CONFIG_ROOT / wt.name / _MANIFEST

    def initialize(self, wt: Worktree) -> None:
        """Record the sandbox name host-side, outside every sandbox mount."""
        manifest = self._manifest_file(wt)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schemaVersion": _MANIFEST_SCHEMA_VERSION,
            "workspace": str(wt.path),
            "sandbox": self._slug(wt),
        }
        tmp = manifest.with_name(f".{manifest.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(manifest)

    def _recorded_sandbox(self, wt: Worktree) -> str:
        """The sandbox name from the host-owned manifest; RuntimeError when it
        is missing or inconsistent (callers decide how loud to be)."""
        manifest = self._manifest_file(wt)
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RuntimeError(f"{manifest} is missing (manually deleted metadata)") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read {manifest}: {exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("schemaVersion") != _MANIFEST_SCHEMA_VERSION
            or data.get("workspace") != str(wt.path)
            or data.get("sandbox") != self._slug(wt)
        ):
            raise RuntimeError(f"{manifest} does not describe this worktree's sandbox")
        return str(data["sandbox"])

    # --- sbx queries -------------------------------------------------------------

    def _list(self) -> dict[str, dict[str, Any]]:
        proc = self._cli(["ls", "--json"])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"sbx ls failed: {detail}" if detail else "sbx ls failed")
        try:
            rows = json.loads(proc.stdout)["sandboxes"]
            return {str(r["name"]): r for r in rows}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"unexpected `sbx ls --json` output: {exc}") from exc

    @staticmethod
    def _owns(row: dict[str, Any], wt: Worktree) -> bool:
        workspaces = row.get("workspaces") or []
        return bool(workspaces) and workspaces[0] == str(wt.path)

    # --- preflight ---------------------------------------------------------------

    def preflight(self, reporter: Reporter) -> None:
        if sys.platform == "darwin" and platform.machine() != "arm64":
            raise PreflightError(
                "sbx isolation on macOS requires Apple silicon.",
                error_code="UNSUPPORTED_PLATFORM",
                hint="Use --isolation docker or host on this machine.",
            )
        if not system.have("sbx"):
            raise PreflightError("sbx runner needs sbx on PATH.", hint=_INSTALL_HINT)
        try:
            self._list()
        except RuntimeError as exc:
            raise PreflightError(
                f"sbx is installed but not usable: {exc}",
                error_code="SBX_UNAVAILABLE",
                hint="Run `sbx login` (and `sbx diagnose` if it persists).",
            ) from exc
        reporter.info("runner: sbx (daemon reachable)")

    def facts(self) -> RunnerFacts:
        return _FACTS

    # --- argv builders -----------------------------------------------------------

    def _mounts(self, wt: Worktree) -> list[str]:
        """Workspaces for ``sbx create``: the worktree (primary, read-write),
        its git common dir, and ``hooks/`` nested read-only."""
        common = git.common_dir(wt.repo)
        paths = [wt.path, common, common / "hooks"]
        for p in paths:
            if ":" in str(p):
                raise RuntimeError(
                    f"Cannot mount a path containing ':': {p} "
                    "(sbx workspace syntax cannot express it). Rename the path and re-create."
                )
        return [str(wt.path), str(common), f"{common / 'hooks'}:ro"]

    def _create_command(self, wt: Worktree) -> list[str]:
        argv = ["sbx", "create", "--name", self._slug(wt)]
        if self.config.sbx_cpus is not None:
            argv += ["--cpus", str(self.config.sbx_cpus)]
        if self.config.sbx_memory is not None:
            argv += ["--memory", self.config.sbx_memory]
        return [*argv, self.config.harness, *self._mounts(wt)]

    # --- setup -------------------------------------------------------------------

    def setup(self, wt: Worktree, *, cold: bool, reporter: Reporter) -> None:
        # Idempotent: `enter` re-runs setup when the lockfile hash changed.
        existing = self._list().get(self._slug(wt))
        if existing is not None:
            if not self._owns(existing, wt):
                raise RuntimeError(
                    f"A sandbox named '{self._slug(wt)}' exists but was not created for "
                    f"{wt.path}. Remove it (`sbx rm {self._slug(wt)}`) and re-create."
                )
            reporter.note("sandbox", f"reusing {self._slug(wt)}")
            return
        (git.common_dir(wt.repo) / "hooks").mkdir(parents=True, exist_ok=True)
        reporter.step("sandbox", "created (dependencies install inside)", self._create_command(wt))

    def refresh(self, wt: Worktree, *, reporter: Reporter) -> None:
        # sbx owns agent auth (login inside the sandbox); nothing to stage.
        return

    def dry_run_setup(self, wt: Worktree) -> list[str]:
        return [shlex.join(self._create_command(wt))]

    # --- entry -------------------------------------------------------------------

    def prepare_entry(self, wt: Worktree) -> None:
        """Verify the sandbox still exists. A stopped sandbox needs no action:
        ``sbx run --name`` restarts it, so the emitted entry_command is never
        dead on arrival."""
        name = self._slug(wt)
        row = self._list().get(name)
        if row is None or not self._owns(row, wt):
            raise RuntimeError(
                f"No sandbox exists for this worktree - re-create it: "
                f"treebox teardown {wt.name} && treebox create {wt.name} --isolation {self.name}"
            )

    def entry_command(self, wt: Worktree, *, harness: Harness, args: list[str]) -> list[str]:
        # sbx launches the agent in its own autonomous mode; only user args are
        # forwarded (after `--`), not the harness's host-side autonomous argv.
        cmd = ["sbx", "run", "--name", self._slug(wt)]
        return [*cmd, "--", *args] if args else cmd

    def launch(self, wt: Worktree, *, harness: Harness, args: list[str]) -> int:
        self.prepare_entry(wt)
        return subprocess.run(self.entry_command(wt, harness=harness, args=args)).returncode

    # --- teardown ----------------------------------------------------------------

    def teardown(self, wt: Worktree, *, reporter: Reporter) -> RunnerTeardownResult:
        """Remove the recorded sandbox, best-effort. Only a sandbox named by
        the host-owned manifest - and still bound to this worktree - is
        removed."""
        manifest_dir = self._manifest_file(wt).parent
        try:
            name = self._recorded_sandbox(wt)
        except RuntimeError as exc:
            reporter.warn(f"sandbox removal skipped: ownership manifest unavailable: {exc}")
            return RunnerTeardownResult.skipped()
        if not system.have("sbx"):
            reporter.note("sandbox", "skipped; sbx unavailable")
            return RunnerTeardownResult.skipped()

        try:
            row = self._list().get(name)
        except RuntimeError as exc:
            reporter.warn(f"sandbox {name}: {exc}; remove manually with `sbx rm {name}`")
            return RunnerTeardownResult("failed")
        failed = False
        if row is None:
            reporter.note("sandbox", "none found")
        elif not self._owns(row, wt):
            reporter.warn(f"sandbox {name} is not bound to {wt.path}; not removing it")
            failed = True
        else:
            proc = self._cli(["rm", "--force", name])
            if proc.returncode == 0:
                reporter.ok("sandbox", f"removed {name}")
            else:
                detail = (proc.stderr or proc.stdout or "").strip()
                reporter.warn(f"sbx rm {name}: {detail}" if detail else f"sbx rm {name} failed")
                failed = True
        if not failed:
            shutil.rmtree(manifest_dir, ignore_errors=True)
        return RunnerTeardownResult("failed" if failed else "cleaned")
