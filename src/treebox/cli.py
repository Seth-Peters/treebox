"""Typer CLI: create / enter / list / teardown / template / doctor / version.

Conventions: data to stdout, diagnostics to stderr. Successful non-launch paths
exit 0; normal ``create`` / ``enter`` launch mode exits with the agent process's
code. ``--json`` gives machine output; ``--print`` emits the copy-pasteable
launch command; both provision without launching the agent (handy over SSH / for
scripting).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple, TypedDict

import typer
from rich.text import Text

from . import (
    __version__,
    assets,
    cleanup,
    forge,
    git,
    lifecycle,
    locking,
    names,
    ports,
    provision,
    record,
    resolve,
    resources,
    status,
    system,
)
from .config import Config, load_config, validate_config
from .harnesses import HARNESSES, VALID_HARNESSES, CredentialStageError, Harness, get_harness
from .models import (
    PLACEHOLDER_PREFIX,
    DepsFreshness,
    TemplateRow,
    Worktree,
    WorktreeRow,
    derive_name,
    expand_user,
    is_placeholder,
    is_valid_name,
    is_valid_template_name,
    path_is_under,
    path_ref,
    placeholder_branch,
    same_path,
    worktree_id,
    worktree_path,
    worktree_root,
)
from .output import (
    DoctorCheck,
    Reporter,
    StepError,
    format_age,
    format_elapsed,
    trim_line_ends,
)
from .resources import ResourceLimitError, ResourceLimits
from .runners import (
    VALID_ISOLATION,
    DockerRunner,
    FirewallError,
    HostRunner,
    PreflightError,
    Runner,
    SandboxError,
    get_runner,
)
from .runners.docker import has_resource_manifest

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOTFOUND = 3
EXIT_PERMISSION = 4
EXIT_CONFLICT = 5

SCHEMA_VERSION = 1

_EPILOG = (
    "Exit codes: 0 ok · 1 runtime (incl. failed setup, incomplete teardown)/doctor "
    "hard-check · 2 usage · 3 not-found · 4 auth/permission · 5 conflict "
    "(exists/locked/dirty). A launch exits with the agent's exit code (non-zero: "
    "the last stderr line is 'treebox: agent exited with status N'); a "
    "SIGINT/SIGTERM/SIGHUP to treebox stops the agent and exits 128+signal."
)

app = typer.Typer(
    add_completion=True,
    no_args_is_help=True,
    help="Isolated, ready-to-run git worktrees for AI coding agents "
    "— host-native or docker-sandboxed.",
    epilog=_EPILOG,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _emit_json(payload: Mapping[str, Any], *, stream: IO[str] | None = None) -> None:
    """The one place --json serialization is defined: pretty-printed, trailing
    newline — identical for success payloads (stdout) and error payloads (stderr)."""
    (stream or sys.stdout).write(json.dumps(payload, indent=2) + "\n")


def _die(
    reporter: Reporter,
    message: str,
    *,
    code: int = EXIT_ERROR,
    error_code: str = "ERROR",
    hint: str | None = None,
    path: str | None = None,
    json_out: bool = False,
) -> typer.Exit:
    """Report a failure and return a typer.Exit carrying the right exit code.

    In ``--json`` mode the error is emitted as a structured object on stderr so
    agents can branch on ``error.code``; otherwise it's a styled message (+ hint).
    """
    if json_out:
        error: dict[str, str] = {"code": error_code, "message": message}
        if hint:
            error["hint"] = hint
        if path:
            error["path"] = path
        payload = {"schemaVersion": SCHEMA_VERSION, "error": error}
        _emit_json(payload, stream=sys.stderr)
    else:
        reporter.error(message)
        if hint:
            reporter.hint(hint)
    return typer.Exit(code)


def _resolve_config(
    reporter: Reporter,
    *,
    isolation: str | None,
    harness: str | None,
    base: str | None,
    root: str | None,
    firewall: bool | None,
    template: str | None = None,
    json_out: bool = False,
) -> Config:
    try:
        cfg = load_config()
    except (TypeError, ValueError) as exc:
        raise _die(
            reporter,
            str(exc),
            code=EXIT_USAGE,
            error_code="INVALID_CONFIG",
            hint="Fix the config file (or unset $TREEBOX_CONFIG).",
            json_out=json_out,
        ) from exc
    cfg = cfg.with_overrides(
        isolation=isolation,
        harness=harness,
        base=base,
        root=root,
        firewall=firewall,
        template=template,
    )
    try:
        validate_config(cfg)
    except ValueError as exc:
        raise _die(
            reporter,
            str(exc),
            code=EXIT_USAGE,
            error_code="INVALID_CONFIG",
            json_out=json_out,
        ) from exc
    return cfg


def _reconcile_with_record(
    reporter: Reporter,
    cfg: Config,
    rec: record.WorktreeRecord,
    *,
    isolation: str | None,
    harness: str | None = None,
    template: str | None = None,
    json_out: bool = False,
) -> Config:
    """Fold a worktree's host-owned record (its creation policy) into the
    session config.

    The record describes the worktree that actually exists, so it beats the
    config default. What an explicit flag means differs per field:

    - ``isolation``: the sandbox/no-sandbox decision is fixed at create time.
      An explicit ``--isolation`` that disagrees with the recorded one is a
      conflict, never an override.
    - ``firewall``: the recorded choice wins - capabilities are fixed on the
      created container, and there is no ``enter`` flag to reconcile.
    - ``harness``: the recorded value wins over the config default, but an
      explicit ``--harness`` stays a legitimate per-session override.
    - ``template``: the template decides the container's user and mounts, so
      the recorded choice wins over the config default; an explicit
      ``--template`` stays a legitimate per-session operator override.
    - ``resources``: the recorded limits are what the sandbox has, so they
      always win (``_check_entry_resources`` refuses a conflicting flag and
      warns about a config or template change).

    ``record.load`` already refused unknown isolation and harness names, so
    the recorded ``str`` names are final here: callers resolve
    ``Harness``/runner objects from the returned config, once.
    """
    if isolation is not None and isolation != rec.isolation:
        raise _die(
            reporter,
            f"Worktree was provisioned with '{rec.isolation}' isolation, "
            f"but --isolation {isolation} was given.",
            code=EXIT_CONFLICT,
            error_code="ISOLATION_MISMATCH",
            hint=f"Drop --isolation to use the recorded '{rec.isolation}' mode, or tear "
            "down and re-create the worktree with the new one.",
            json_out=json_out,
        )
    cfg = cfg.with_overrides(
        isolation=rec.isolation, firewall=rec.firewall, resources=rec.resources
    )
    if harness is None:
        cfg = cfg.with_overrides(harness=rec.harness)
    if template is None:
        cfg = cfg.with_overrides(template=rec.template)
    return cfg


def _record_hint(exc: record.RecordError) -> str:
    """The recovery for a missing or invalid host-owned record: tear the
    worktree down and create it again. Never a fallback to config defaults or
    to the worktree's sandbox-writable state."""
    return (
        f"Tear it down and create it again: treebox teardown {path_ref(exc.worktree)}, "
        "then treebox create (add --checkout <branch> to keep its branch)."
    )


_CPUS_HELP = (
    "CPU limit for this worktree's sandbox, in CPUs (a decimal, at least 0.01, "
    "at most the engine's CPU count), e.g. --cpus 2 or --cpus 1.5. Maps to "
    "docker run --cpus. Overrides \\[resources] cpus in config and the template's "
    "resources. Unset: no treebox cap. Fixed when the sandbox is created. "
    "--isolation host has no sandbox and refuses this flag (exit 2)."
)
_MEMORY_HELP = (
    "Memory limit for this worktree's sandbox, in Docker's memory syntax: a "
    "number and a unit b, k, m, g, t or p (binary multiples; a bare number is "
    "bytes; at least 6m), e.g. --memory 4g or --memory 512m. Maps to docker run "
    "--memory. Overrides \\[resources] memory in config and the template's "
    "resources. Unset: no treebox cap. Fixed when the sandbox is created. "
    "--isolation host has no sandbox and refuses this flag (exit 2)."
)
_PIDS_HELP = (
    "Process limit for this worktree's sandbox: the most processes and threads "
    "it can run at once (a positive whole number), e.g. --pids-limit 512. Maps "
    "to docker run --pids-limit. Overrides \\[resources] pids in config and the "
    "template's resources. Unset: no treebox cap. Fixed when the sandbox is "
    "created. --isolation host has no sandbox and refuses this flag (exit 2)."
)
_ENTER_LIMIT_NOTE = (
    " On enter: only restates the recorded limit; a different value is a "
    "conflict (exit 5) with the re-create commands."
)


def _resource_flags(
    reporter: Reporter,
    *,
    cpus: str | None,
    memory: str | None,
    pids_limit: str | None,
    json_out: bool,
) -> ResourceLimits:
    """Validate the ``--cpus`` / ``--memory`` / ``--pids-limit`` flags before
    anything is provisioned: a bad value is a usage error (exit 2)."""
    try:
        return resources.from_flags(cpus=cpus, memory=memory, pids=pids_limit)
    except ResourceLimitError as exc:
        raise _die(
            reporter,
            str(exc),
            code=EXIT_USAGE,
            error_code="INVALID_RESOURCE",
            hint="See the units and an example in `treebox create --help`.",
            json_out=json_out,
        ) from exc


def _refuse_unsupported_limits(
    reporter: Reporter, run: Runner, explicit: ResourceLimits, *, json_out: bool
) -> None:
    """An explicit limit the runner cannot apply is an error, never ignored."""
    unsupported = [n for n in explicit.names() if n not in run.supported_resources]
    if not unsupported:
        return
    flags = ", ".join(resources.FLAGS[n] for n in unsupported)
    raise _die(
        reporter,
        f"The {run.name} runner cannot apply {flags}"
        + (
            ": it runs the agent on the host, with no sandbox to cap."
            if run.name == "host"
            else "."
        ),
        code=EXIT_USAGE,
        error_code="RESOURCE_UNSUPPORTED",
        hint=f"Drop {flags}, or use a sandbox: --isolation docker.",
        json_out=json_out,
    )


def _creation_limits(
    reporter: Reporter,
    cfg: Config,
    run: Runner,
    explicit: ResourceLimits,
    *,
    json_out: bool,
) -> tuple[ResourceLimits, list[str]]:
    """The effective limits of a new sandbox, field by field: explicit flag,
    then user config (``cfg.resources``), then the runner's template tier,
    restricted to what the runner can apply. Also returns the config limits
    this runner does not apply (shown as a note, never an error: one
    ``[resources]`` table must not break every host worktree). This needs no
    engine; a value the engine itself refuses is checked after the runner
    preflight (``_run_session``), before provisioning."""
    _refuse_unsupported_limits(reporter, run, explicit, json_out=json_out)
    try:
        wanted = explicit.over(cfg.resources).over(run.template_resources())
    except _PROVISION_ERRORS as exc:
        raise _handle(reporter, exc, json_out=json_out) from exc
    effective = wanted.only(run.supported_resources)
    ignored = [n for n in cfg.resources.names() if n not in run.supported_resources]
    return effective, ignored


def _note_ignored_limits(reporter: Reporter, run: Runner, ignored: list[str]) -> None:
    if ignored:
        reporter.note(
            "limits",
            f"[resources] {', '.join(ignored)} not applied: the {run.name} runner "
            "sets no such limit",
        )


def _recreate_command(rec: record.WorktreeRecord, branch: str | None, flags: ResourceLimits) -> str:
    """The exact commands that re-create a worktree's sandbox. ``flags`` are
    the limits to choose by flag; every other limit comes from config and the
    template again, as on any create (teardown keeps the branch, and its
    commits, by default)."""
    name = rec.worktree.name
    create = ["treebox", "create", name]
    if branch and branch != "detached":
        create += ["--checkout", branch]
    create += ["--isolation", rec.isolation]
    if rec.firewall:
        create.append("--firewall")
    create += ["--template", rec.template, "-H", rec.harness, *flags.cli_flags()]
    return f"treebox teardown {path_ref(rec.worktree)} && {shlex.join(create)}"


def _check_entry_resources(
    reporter: Reporter,
    run: Runner,
    rec: record.WorktreeRecord,
    *,
    branch: str | None,
    explicit: ResourceLimits,
    config_limits: ResourceLimits,
    json_out: bool,
) -> ResourceLimits:
    """The recorded limits are what an existing sandbox has, and a container's
    limits cannot change in place. An explicit flag that differs is a
    conflict (exit 5) with the re-create commands. A limit that config or the
    template gave at create, and that they now give differently, is drift: a
    warning with the same commands, never a silent no-op. Returns the drift
    (the values config and template now give; unset = no drift).

    A limit an explicit ``create`` flag chose (``rec.explicit_resources``)
    never drifts: the operator picked it on purpose. Neither does one this
    ``enter`` restates by flag."""
    _refuse_unsupported_limits(reporter, run, explicit, json_out=json_out)
    recorded = rec.resources
    chosen = recorded.only(rec.explicit_resources)
    differs = [n for n in explicit.names() if getattr(explicit, n) != getattr(recorded, n)]
    if differs:
        flags = ", ".join(
            f"{resources.FLAGS[n]} {resources.format_value(n, getattr(explicit, n))}"
            for n in differs
        )
        raise _die(
            reporter,
            f"Worktree '{rec.worktree.name}' was created with limits "
            f"{recorded.describe()}; {flags} cannot apply to its existing sandbox.",
            code=EXIT_CONFLICT,
            error_code="RESOURCE_MISMATCH",
            hint="Drop the flag to keep the recorded limits, or commit your work and "
            "re-create the sandbox: " + _recreate_command(rec, branch, explicit.over(chosen)),
            path=str(record.record_path(rec.worktree)),
            json_out=json_out,
        )
    try:
        template = run.template_resources()
    except _PROVISION_ERRORS:
        template = ResourceLimits()
    wanted = config_limits.over(template).only(run.supported_resources)
    pinned = rec.explicit_resources | set(explicit.names())
    drift = wanted.only(
        n for n in wanted.names() if n not in pinned and getattr(wanted, n) != getattr(recorded, n)
    )
    if drift.names():
        reporter.warn(
            f"limits: this sandbox keeps its recorded limits ({recorded.describe()}); "
            f"config and template now give {drift.describe()}. A limit change cannot "
            "apply to an existing sandbox. To apply it, commit your work and "
            f"re-create: {_recreate_command(rec, branch, chosen)}"
        )
    return drift


def _repo_root(reporter: Reporter, repo: str, *, json_out: bool = False) -> str:
    try:
        return git.main_worktree(expand_user(repo))
    except git.GitMissingError as exc:
        raise _die(
            reporter,
            str(exc),
            code=EXIT_ERROR,
            error_code="GIT_MISSING",
            hint=_GIT_MISSING_HINT,
            json_out=json_out,
        ) from exc
    except git.GitError as exc:
        raise _die(
            reporter,
            str(exc),
            code=EXIT_USAGE,
            error_code="NOT_A_REPO",
            hint="Run inside a git repo, or pass --repo <path>.",
            json_out=json_out,
        ) from exc


def _check_root(reporter: Reporter, repo_path: str, root: str, *, json_out: bool = False) -> None:
    """Refuse a worktree root that cannot hold worktrees: the root, or its
    nearest existing ancestor, is not a directory (a regular file, a dangling
    symlink). A root that does not exist yet is fine - create makes it."""
    base = worktree_root(repo_path, root)
    probe = base
    while not os.path.lexists(probe) and probe != probe.parent:
        probe = probe.parent
    if probe.is_dir():
        return
    where = "" if probe == base else f" ({probe} is not a directory)"
    raise _die(
        reporter,
        f"Worktree root {base} is not a directory{where}.",
        code=EXIT_USAGE,
        error_code="INVALID_ROOT",
        hint="Point --root (or root in config.toml) at a directory, "
        "or at a path that does not exist yet.",
        path=str(base),
        json_out=json_out,
    )


def _validate_branch(reporter: Reporter, branch: str, *, json_out: bool = False) -> None:
    if not git.check_ref_format(branch):
        raise _die(
            reporter,
            f"Invalid branch name '{branch}'.",
            code=EXIT_USAGE,
            error_code="INVALID_BRANCH",
            json_out=json_out,
        )


def _validate_name(reporter: Reporter, name: str, *, json_out: bool = False) -> None:
    if name.startswith(PLACEHOLDER_PREFIX):
        raise _die(
            reporter,
            f"Invalid worktree name '{name}': the {PLACEHOLDER_PREFIX} prefix is "
            "reserved for generated placeholder branches.",
            code=EXIT_USAGE,
            error_code="INVALID_NAME",
            hint="The name becomes the branch name, and treebox/* refs are un-pushable "
            "by design — pick a name without the prefix.",
            json_out=json_out,
        )
    if not is_valid_name(name):
        raise _die(
            reporter,
            f"Invalid worktree name '{name}'.",
            code=EXIT_USAGE,
            error_code="INVALID_NAME",
            hint="Lowercase slug tokens (letters, digits, hyphens) separated by slashes, "
            "e.g. fix-auth or feature/user-auth — or omit it for a generated name. "
            "The name is used as the branch name; slashes become -- in the directory.",
            json_out=json_out,
        )


def _name_taken(repo_path: str, root: str, name: str) -> bool:
    """Whether a *generated* name is unusable: its directory or its placeholder
    branch (local or on origin) already exists."""
    if worktree_path(repo_path, root, name).exists():
        return True
    ph = placeholder_branch(name)
    return git.local_branch_exists(repo_path, ph) or git.remote_branch_exists(repo_path, ph)


_PROVISION_ERRORS = (
    provision.ProvisionError,
    locking.LockError,
    git.GitError,
    StepError,
    RuntimeError,
)


class _ErrorInfo(NamedTuple):
    """How a provisioning exception surfaces: exit code, stable machine code
    (for --json consumers), and the optional remediation hint."""

    exit_code: int
    error_code: str
    hint: str | None


_GIT_MISSING_HINT = "Install git and ensure it is on PATH."


def _classify(exc: Exception) -> _ErrorInfo:
    """Map a provisioning exception to its exit code / error code / hint."""
    if isinstance(exc, provision.DestinationConflictError):
        return _ErrorInfo(EXIT_CONFLICT, "DESTINATION_CONFLICT", exc.hint)
    if isinstance(exc, provision.SubmoduleCopyFailedError):
        return _ErrorInfo(EXIT_ERROR, "SUBMODULE_COPY_FAILED", exc.hint)
    if isinstance(exc, git.GitMissingError):
        return _ErrorInfo(EXIT_ERROR, "GIT_MISSING", _GIT_MISSING_HINT)
    if isinstance(exc, locking.LockError):
        return _ErrorInfo(
            EXIT_CONFLICT,
            "LOCK_HELD",
            exc.hint or "Another run holds this worktree — wait, then retry.",
        )
    if isinstance(exc, ports.PortsExhaustedError):
        return _ErrorInfo(EXIT_CONFLICT, "PORTS_EXHAUSTED", exc.hint)
    if isinstance(exc, ports.LedgerInvalidError):
        return _ErrorInfo(EXIT_ERROR, "PORT_LEDGER_INVALID", exc.hint)
    if isinstance(exc, git.FetchError):
        msg = str(exc).lower()
        if "publickey" in msg or "permission denied" in msg:
            hint = (
                "Git auth failed. Authenticate once and re-run: `gh auth login` "
                "(GitHub), `glab auth login` (GitLab), or a git credential helper / "
                "HTTPS token (Bitbucket, Azure, others). Or load an SSH key "
                '(eval "$(ssh-agent -s)"; ssh-add), or pass --no-fetch for stale refs.'
            )
        else:
            hint = (
                "Make origin reachable: authenticate once (`gh auth login`, "
                "`glab auth login`, or a git credential helper), or pass --no-fetch."
            )
        return _ErrorInfo(EXIT_PERMISSION, "FETCH_FAILED", hint)
    if isinstance(exc, provision.SlugConflictError):
        return _ErrorInfo(EXIT_CONFLICT, "SLUG_CONFLICT", exc.hint)
    if isinstance(exc, provision.InvalidLinkageError):
        return _ErrorInfo(EXIT_CONFLICT, "INVALID_LINKAGE", exc.hint)
    if isinstance(exc, provision.SetupFailedError):
        return _ErrorInfo(EXIT_ERROR, "SETUP_FAILED", exc.hint)
    if isinstance(exc, record.RecordMissingError):
        return _ErrorInfo(EXIT_CONFLICT, "RECORD_MISSING", _record_hint(exc))
    if isinstance(exc, record.RecordInvalidError):
        return _ErrorInfo(EXIT_CONFLICT, "RECORD_INVALID", _record_hint(exc))
    if isinstance(exc, provision.RecordConflictError):
        return _ErrorInfo(EXIT_CONFLICT, "RECORD_CONFLICT", exc.hint)
    if isinstance(exc, assets.TemplateNameError):
        return _ErrorInfo(
            EXIT_USAGE, "INVALID_CONFIG", "Pass a template name, e.g. --template default."
        )
    if isinstance(exc, provision.BranchInUseError):
        return _ErrorInfo(EXIT_CONFLICT, "BRANCH_IN_USE", exc.hint)
    if isinstance(exc, provision.BranchConflictError):
        return _ErrorInfo(EXIT_CONFLICT, "BRANCH_EXISTS", exc.hint)
    if isinstance(exc, resolve.AmbiguousRefError):
        return _ErrorInfo(EXIT_USAGE, "AMBIGUOUS_REF", exc.hint)
    if isinstance(exc, provision.NotFoundError):
        return _ErrorInfo(EXIT_NOTFOUND, "NOT_FOUND", exc.hint)
    if isinstance(exc, assets.TemplateNotFoundError):
        return _ErrorInfo(
            EXIT_NOTFOUND,
            "TEMPLATE_NOT_FOUND",
            "Run 'treebox template list' to see available templates, or scaffold "
            f"this one: treebox template init {exc.name}",
        )
    if isinstance(exc, assets.TemplateInvalidError):
        return _ErrorInfo(
            EXIT_ERROR,
            "TEMPLATE_INVALID",
            f"Inspect the template dir: treebox template path {exc.name}",
        )
    if isinstance(exc, PreflightError):
        return _ErrorInfo(EXIT_ERROR, exc.error_code, exc.hint)
    if isinstance(exc, FirewallError):
        return _ErrorInfo(EXIT_ERROR, "FIREWALL_FAILED", exc.hint)
    if isinstance(exc, SandboxError):
        return _ErrorInfo(EXIT_ERROR, "SANDBOX_FAILED", exc.hint)
    if isinstance(exc, CredentialStageError):
        return _ErrorInfo(EXIT_ERROR, "CREDENTIAL_STAGE_FAILED", exc.hint)
    return _ErrorInfo(EXIT_ERROR, "ERROR", None)


def _handle(reporter: Reporter, exc: Exception, *, json_out: bool) -> typer.Exit:
    reporter.restore_terminal()
    code, error_code, hint = _classify(exc)
    return _die(
        reporter,
        str(exc),
        code=code,
        error_code=error_code,
        hint=hint,
        path=str(exc.path)
        if isinstance(exc, (provision.ProvisionError, record.RecordError, ports.PortsError))
        and exc.path
        else None,
        json_out=json_out,
    )


def _short_path(path: Path | str, repo: str) -> str:
    """A compact, readable path: relative to the repo when possible, else
    home-collapsed — so status rows stay on one tidy line."""
    p = Path(path)
    try:
        return str(p.relative_to(repo))
    except ValueError:
        pass
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


def _emit_result(
    outcome: provision.Outcome,
    *,
    json_out: bool,
    print_only: bool,
    warnings: list[str],
    limits: ResourceLimits,
    drift: ResourceLimits | None = None,
) -> None:
    """Write the machine/script-facing result to stdout. ``warnings`` are the
    run's warnings (an additive payload field: empty when nothing was
    skipped). ``drift`` (``enter`` only) is what config and template now give
    for a limit the sandbox keeps: ``resources_drift``, every key present,
    ``null`` = no drift."""
    block = outcome.worktree.ports
    if json_out:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "name": outcome.worktree.name,
            "worktree_path": str(outcome.worktree.path),
            "id": outcome.worktree.id,
            "port_base": block.base if block else None,
            "port_count": block.count if block else None,
            "branch": outcome.worktree.branch,
            "base": outcome.worktree.base,
            "entry_command": outcome.entry_command,
            "created": outcome.created,
            "resources": limits.to_json(),
            "warnings": warnings,
        }
        if drift is not None:
            payload["resources_drift"] = drift.to_json()
        _emit_json(payload)
    elif print_only:
        sys.stdout.write(" ".join(shlex.quote(p) for p in outcome.entry_command) + "\n")


def _dry_run(
    reporter: Reporter,
    cfg: Config,
    run: Runner,
    repo_path: str,
    name: str,
    branch: str,
    *,
    fetch: bool,
    cold: bool,
    json_out: bool,
    checkout: str | None,
    ignored_limits: list[str],
) -> None:
    """Render what ``create`` would do, executing nothing."""
    try:
        wt, cmds = provision.dry_run_plan(
            cfg,
            run,
            repo=repo_path,
            name=name,
            branch=branch,
            base=cfg.base,
            fetch=fetch,
            cold=cold,
            existing_branch=checkout is not None,
        )
    except _PROVISION_ERRORS as exc:
        raise _handle(reporter, exc, json_out=json_out) from exc

    if json_out:
        _emit_json(
            {
                "schemaVersion": SCHEMA_VERSION,
                "dry_run": True,
                "name": name,
                "worktree_path": str(wt.path),
                "branch": branch,
                "commands": cmds,
                "resources": cfg.resources.to_json(),
                "warnings": reporter.warnings,
            }
        )
        return

    reporter.heading("create", f"{name}  ·  dry run")
    reporter.summary("worktree", _short_path(wt.path, repo_path))
    reporter.summary("branch", branch)
    reporter.summary("isolation", f"{cfg.isolation}  →  {cfg.harness}")
    if cfg.resources.names():
        reporter.summary("limits", cfg.resources.describe())
    _note_ignored_limits(reporter, run, ignored_limits)
    reporter.blank()
    for cmd in cmds:
        reporter.command(cmd)
    reporter.blank()


@app.command()
def create(
    name: str | None = typer.Argument(
        None,
        help=(
            "Worktree name, used directly as the branch name: lowercase slug tokens "
            "(letters, digits, hyphens) separated by slashes, e.g. fix-auth or "
            "feature/user-auth (the directory flattens slashes to --). "
            "Omitted: a generated petname on a guarded treebox/ placeholder branch."
        ),
    ),
    checkout: str | None = typer.Option(
        None,
        "--checkout",
        help="Check out this exact existing branch (local or origin) instead of "
        "creating a new one.",
    ),
    repo: str = typer.Option(".", "--repo", help="Git repo to create from. Default: current repo."),
    root: str | None = typer.Option(None, "--root", help="Worktree root dir."),
    base: str | None = typer.Option(
        None, "--base", help="Base branch for the new branch (resolved as origin/<base>)."
    ),
    isolation: str | None = typer.Option(
        None,
        "--isolation",
        help=f"Isolation mode: {'|'.join(VALID_ISOLATION)}. docker needs a non-root user.",
    ),
    harness: str | None = typer.Option(
        None, "--harness", "-H", help=f"Agent harness to launch: {'|'.join(VALID_HARNESSES)}."
    ),
    cold: bool = typer.Option(
        False, "--cold", help="Bypass shared caches for a from-source build."
    ),
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help="Opt out of the required origin fetch and accept (possibly stale) local refs.",
    ),
    firewall: bool | None = typer.Option(
        None,
        "--firewall/--no-firewall",
        help="Enable/disable the container firewall (docker isolation). "
        "Unset: the config default applies.",
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        help="Operator-owned sandbox template name (docker isolation). "
        "Resolved from $TREEBOX_TEMPLATE_DIR or $TREEBOX_HOME/templates/<name> "
        "(default ~/.treebox/templates/<name>).",
    ),
    cpus: str | None = typer.Option(None, "--cpus", help=_CPUS_HELP, metavar="CPUS"),
    memory: str | None = typer.Option(None, "--memory", help=_MEMORY_HELP, metavar="SIZE"),
    pids_limit: str | None = typer.Option(None, "--pids-limit", help=_PIDS_HELP, metavar="N"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Print the git/setup commands that would run; change nothing.",
    ),
    print_only: bool = typer.Option(
        False, "--print", help="Provision, then print the launch command (no launch)."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Provision, then print a JSON result (no launch)."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Stream raw command output (to stderr)."
    ),
    args: list[str] | None = typer.Argument(
        None,
        help="Extra args passed to the agent, after -- and after NAME "
        "(create NAME -- ARGS...), exactly as for enter. With --json or "
        "--print they are part of the emitted entry_command.",
        show_default=False,
    ),
) -> None:
    """Provision a worktree and launch the agent harness in it.

    An explicit NAME becomes the branch, created fresh from origin/<base>
    (feature/auth lives in directory feature--auth). Without NAME or
    --checkout, the worktree starts on a generated treebox/<petname>
    placeholder branch: name the work when it takes shape
    (git branch -m <type>/<short-name>, e.g. feature/user-auth, fix/login-race,
    chore/bump-deps), then push. Every worktree gets a pre-push guard that
    keeps treebox/* refs un-pushable.

    A failed setup step fails create (exit 1, SETUP_FAILED: the step, its
    exit status, and the file with its output). The worktree is kept and
    marked unfinished; `treebox enter NAME` or the same create retries setup.

    Setup and the agent get, for repo-owned service scripts: TREEBOX_NAME
    (the worktree name), TREEBOX_ID (10 lowercase hex characters, unique on
    this machine, derived from the worktree path), and TREEBOX_PORT_BASE and
    TREEBOX_PORT_COUNT (a port block no other treebox worktree on the machine
    has; the ports table of the user config sets the range). --print and
    --json commands carry them too. A full range fails before anything is made
    (exit 5, PORTS_EXHAUSTED); --dry-run reserves nothing.

    To start the agent on a task, put its args after NAME and --
    (create fix-auth -- -p "fix the login race"). A nameless create takes
    no agent args: give a NAME.

    When it launches (no --json, --print, --dry-run), create exits with the
    agent's exit code; a non-zero one is named in the last stderr line
    ("treebox: agent exited with status N"). SIGINT, SIGTERM or SIGHUP to
    treebox stops the agent (inside the container for docker) and exits
    128+signal (CANCELLED).
    """
    reporter = Reporter(quiet=quiet, verbose=verbose, silent=json_out)
    cfg = _resolve_config(
        reporter,
        isolation=isolation,
        harness=harness,
        base=base,
        root=root,
        firewall=firewall,
        template=template,
        json_out=json_out,
    )
    explicit_limits = _resource_flags(
        reporter, cpus=cpus, memory=memory, pids_limit=pids_limit, json_out=json_out
    )
    if checkout is None and name is not None and name.startswith("-"):
        raise _die(
            reporter,
            f"Invalid branch name '{name}'.",
            code=EXIT_USAGE,
            error_code="INVALID_BRANCH",
            hint="A name cannot start with '-'. To pass agent args, name the worktree "
            "first: treebox create NAME -- ARGS...",
            json_out=json_out,
        )
    if name is not None:
        _validate_name(reporter, name, json_out=json_out)
    if checkout is not None:
        _validate_branch(reporter, checkout, json_out=json_out)
    repo_path = _repo_root(reporter, repo, json_out=json_out)
    _check_root(reporter, repo_path, cfg.root, json_out=json_out)

    if checkout is not None:
        wt_name = derive_name(name or checkout)
        target_branch = checkout
    elif name is not None:
        wt_name = derive_name(name)
        target_branch = name
        _validate_branch(reporter, target_branch, json_out=json_out)
    else:
        wt_name = names.petname(lambda n: _name_taken(repo_path, cfg.root, n))
        target_branch = placeholder_branch(wt_name)

    run = get_runner(cfg)

    if cfg.isolation == "docker":
        try:
            assets.load_template_json(cfg.template, assets.CONFIG_FILE)
            if cfg.firewall:
                assets.load_template_json(cfg.template, assets.FIREWALL_FILE)
        except (assets.TemplateNotFoundError, assets.TemplateInvalidError) as exc:
            raise _handle(reporter, exc, json_out=json_out) from exc

    limits, ignored_limits = _creation_limits(
        reporter, cfg, run, explicit_limits, json_out=json_out
    )
    cfg = cfg.with_overrides(
        resources=limits, explicit_resources=frozenset(explicit_limits.names())
    )
    run = get_runner(cfg)

    if dry_run:
        _dry_run(
            reporter,
            cfg,
            run,
            repo_path,
            wt_name,
            target_branch,
            fetch=not no_fetch,
            cold=cold,
            json_out=json_out,
            checkout=checkout,
            ignored_limits=ignored_limits,
        )
        return

    reporter.heading("create", wt_name)
    if checkout is not None:
        reporter.summary("branch", target_branch)
    elif name is not None:
        reporter.summary("branch", target_branch)
        reporter.summary("base", cfg.base)
    else:
        reporter.summary("branch", f"{target_branch}  ·  placeholder — rename before push")
        reporter.summary("base", cfg.base)
    reporter.summary("isolation", f"{cfg.isolation}  →  {cfg.harness}")
    if limits.names():
        reporter.summary("limits", limits.describe())
    if cold:
        reporter.summary("cache", "cold (from source)")
    _note_ignored_limits(reporter, run, ignored_limits)
    reporter.blank()

    agent = get_harness(cfg.harness)

    def _provision() -> provision.Outcome:
        return provision.create(
            cfg,
            run,
            repo=repo_path,
            name=wt_name,
            branch=target_branch,
            base=cfg.base,
            harness=agent,
            cold=cold,
            fetch=not no_fetch,
            interactive=_interactive(),
            reporter=reporter,
            existing_branch=checkout is not None,
            args=args or [],
        )

    _run_session(
        reporter,
        run,
        agent,
        path=worktree_path(repo_path, cfg.root, wt_name),
        provision_call=_provision,
        json_out=json_out,
        print_only=print_only,
        args=args or [],
        limits=limits,
        check_limits=True,
    )


@app.command()
def enter(
    ref: str = typer.Argument(
        ...,
        help="Worktree name, branch, or a unique substring of either; "
        "an absolute path matches only the worktree registered there.",
    ),
    repo: str = typer.Option(".", "--repo", help="Git repo. Default: current repo."),
    root: str | None = typer.Option(None, "--root", help="Worktree root dir."),
    isolation: str | None = typer.Option(
        None,
        "--isolation",
        help=f"Isolation mode: {'|'.join(VALID_ISOLATION)}. docker needs a non-root user.",
    ),
    harness: str | None = typer.Option(
        None, "--harness", "-H", help=f"Agent harness to launch: {'|'.join(VALID_HARNESSES)}."
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        help="Operator-owned sandbox template name (docker isolation). "
        "Resolved from $TREEBOX_TEMPLATE_DIR or $TREEBOX_HOME/templates/<name> "
        "(default ~/.treebox/templates/<name>).",
    ),
    cold: bool = typer.Option(False, "--cold", help="Bypass shared caches when re-syncing."),
    cpus: str | None = typer.Option(
        None, "--cpus", help=_CPUS_HELP + _ENTER_LIMIT_NOTE, metavar="CPUS"
    ),
    memory: str | None = typer.Option(
        None, "--memory", help=_MEMORY_HELP + _ENTER_LIMIT_NOTE, metavar="SIZE"
    ),
    pids_limit: str | None = typer.Option(
        None, "--pids-limit", help=_PIDS_HELP + _ENTER_LIMIT_NOTE, metavar="N"
    ),
    print_only: bool = typer.Option(
        False, "--print", help="Prepare, then print the launch command (no launch)."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Prepare, then print a JSON result (no launch)."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Stream raw command output (to stderr)."
    ),
    args: list[str] | None = typer.Argument(
        None, help="Extra args passed to the agent (after --)."
    ),
) -> None:
    """Re-launch the agent in an existing worktree; refresh .env and re-sync deps if changed.

    An unfinished or failed setup runs again; if a setup step fails, enter
    fails too (exit 1, SETUP_FAILED) and the next enter retries it.

    Setup and the agent get the same TREEBOX_NAME, TREEBOX_ID,
    TREEBOX_PORT_BASE and TREEBOX_PORT_COUNT as at create; --print and --json
    commands carry them too.

    When it launches (no --json, --print), enter exits with the agent's exit
    code; a non-zero one is named in the last stderr line ("treebox: agent
    exited with status N"). SIGINT, SIGTERM or SIGHUP to treebox stops the
    agent (inside the container for docker) and exits 128+signal (CANCELLED).
    """
    reporter = Reporter(quiet=quiet, verbose=verbose, silent=json_out)
    cfg = _resolve_config(
        reporter,
        isolation=isolation,
        harness=harness,
        base=None,
        root=root,
        firewall=None,
        template=template,
        json_out=json_out,
    )
    explicit_limits = _resource_flags(
        reporter, cpus=cpus, memory=memory, pids_limit=pids_limit, json_out=json_out
    )
    config_limits = cfg.resources
    repo_path = _repo_root(reporter, repo, json_out=json_out)
    _check_root(reporter, repo_path, cfg.root, json_out=json_out)
    try:
        cand = resolve.resolve_ref(repo_path, cfg.root, ref)
    except provision.NotFoundError as exc:
        if git.local_branch_exists(repo_path, ref) or git.remote_branch_exists(repo_path, ref):
            exc.hint = (
                f"Branch '{ref}' exists but has no worktree — "
                f"run `treebox create --checkout {ref}`."
            )
        raise _handle(reporter, exc, json_out=json_out) from exc
    except _PROVISION_ERRORS as exc:
        raise _handle(reporter, exc, json_out=json_out) from exc
    try:
        rec = record.load(Path(cand.path))
    except record.RecordError as exc:
        raise _handle(reporter, exc, json_out=json_out) from exc
    cfg = _reconcile_with_record(
        reporter,
        cfg,
        rec,
        isolation=isolation,
        harness=harness,
        template=template,
        json_out=json_out,
    )
    run = get_runner(cfg)
    agent = get_harness(cfg.harness)

    subtitle = cand.name if cand.branch in (None, cand.name) else f"{cand.name}  ·  {cand.branch}"
    reporter.heading("enter", subtitle)
    reporter.summary("isolation", f"{cfg.isolation}  →  {cfg.harness}")
    if rec.resources.names():
        reporter.summary("limits", rec.resources.describe())
    drift = _check_entry_resources(
        reporter,
        run,
        rec,
        branch=cand.branch,
        explicit=explicit_limits,
        config_limits=config_limits,
        json_out=json_out,
    )
    reporter.blank()

    def _provision() -> provision.Outcome:
        return provision.enter(
            cfg,
            run,
            repo=repo_path,
            path=Path(cand.path),
            base=rec.base,
            harness=agent,
            cold=cold,
            args=args or [],
            reporter=reporter,
        )

    _run_session(
        reporter,
        run,
        agent,
        path=Path(cand.path),
        provision_call=_provision,
        json_out=json_out,
        print_only=print_only,
        args=args or [],
        limits=rec.resources,
        drift=drift,
    )


def _run_session(
    reporter: Reporter,
    run: Runner,
    harness: Harness,
    *,
    path: Path,
    provision_call: Callable[[], provision.Outcome],
    json_out: bool,
    print_only: bool,
    args: list[str],
    limits: ResourceLimits,
    drift: ResourceLimits | None = None,
    check_limits: bool = False,
) -> None:
    """The session spine shared by ``create`` and ``enter``: runner preflight,
    the lock on the worktree at ``path``, the provisioning step, one exception
    → exit-code classification, then the ``--json`` / ``--print`` / launch fork.

    Preflight runs first so runner-specific preconditions (a no-op for the
    host runner) fail fast with a clean error + hint — before any git state
    is touched, and never surfacing a stopped docker daemon deep inside
    launch as a misleading "no container" that steers users into a
    destructive teardown. With ``check_limits`` (``create``: ``limits`` are
    the ones a new sandbox gets) the engine's own bounds on them are checked
    next: only after preflight, because that check asks the engine, and a
    missing or stopped engine must be the preflight's classified error. A
    launch also checks the agent first (the harness binary on the host; in
    unattended mode also its login), so a missing prerequisite fails before
    anything is provisioned.

    Launch contract: treebox exits with the agent's exit code. When the
    agent exits non-zero, the last stderr line is ``AGENT_EXIT_PREFIX`` and
    that code, so a caller can tell an agent exit from a treebox failure
    (which always prints its classified error instead). A cancel signal
    (SIGINT, SIGTERM, SIGHUP) stops the agent and exits ``128 + signum``
    with ``CANCELLED``.
    """
    started = time.monotonic()
    launching = not (json_out or print_only)
    try:
        run.preflight(reporter)
        if check_limits:
            run.check_resources(limits)
        if launching:
            _require_agent(run, harness, login=not _interactive())
        with locking.worktree_lock(path):
            outcome = provision_call()
            run.prepare_entry(outcome.worktree)
    except ResourceLimitError as exc:
        raise _die(
            reporter,
            str(exc),
            code=EXIT_USAGE,
            error_code="INVALID_RESOURCE",
            hint="Pick a smaller value, or check the Docker engine's resources (docker info).",
            json_out=json_out,
        ) from exc
    except _PROVISION_ERRORS as exc:
        raise _handle(reporter, exc, json_out=json_out) from exc

    if not launching:
        _emit_result(
            outcome,
            json_out=json_out,
            print_only=print_only,
            warnings=reporter.warnings,
            limits=limits,
            drift=drift,
        )
        return
    reporter.blank()
    reporter.ready(format_elapsed(time.monotonic() - started), harness.name)
    reporter.blank()
    reporter.restore_terminal()
    try:
        code = run.launch(outcome.worktree, harness=harness, args=args)
    except system.SessionCancelled as exc:
        raise _die(
            reporter,
            str(exc),
            code=128 + exc.signum,
            error_code="CANCELLED",
            hint=f"The worktree is kept. Start the agent again: treebox enter "
            f"{path_ref(outcome.worktree.path)}",
        ) from exc
    except RuntimeError as exc:
        raise _handle(reporter, exc, json_out=False) from exc
    if code == 0:
        raise typer.Exit(0)
    status = code if code > 0 else 128 - code
    killed = f" (killed by {system.signal_name(-code)})" if code < 0 else ""
    sys.stderr.write(f"{AGENT_EXIT_PREFIX}{status}{killed}\n")
    sys.stderr.flush()
    raise typer.Exit(status)


AGENT_EXIT_PREFIX = "treebox: agent exited with status "


def _agent_binary_check(run: Runner, harness: Harness) -> DoctorCheck:
    """The harness binary, where the agent runs. On the host it must be on
    PATH; in a sandbox it is part of the image, checked at launch."""
    name = f"agent: {harness.name}"
    if run.facts().sandboxed:
        return DoctorCheck(name, True, f"in the {run.name} sandbox image · checked at launch")
    found = shutil.which(harness.binary())
    if found:
        return DoctorCheck(name, True, found)
    return DoctorCheck(
        name,
        False,
        f"'{harness.binary()}' not found on PATH",
        f"Install the {harness.name} CLI so '{harness.binary()}' is on PATH.",
    )


def _agent_login_check(harness: Harness, *, required: bool = True) -> DoctorCheck:
    """The harness's subscription login: the one an agent on the host uses
    and a sandbox gets a copy of."""
    name = f"login: {harness.name}"
    found = harness.login()
    if found:
        return DoctorCheck(name, True, found, required=required)
    return DoctorCheck(
        name,
        False,
        f"no login at {harness.login_file()}",
        harness.login_hint(),
        required=required,
    )


def _require_agent(run: Runner, harness: Harness, *, login: bool) -> None:
    """Fail a launch before provisioning when the agent cannot start: no
    harness binary (HARNESS_MISSING) or, with ``login``, no usable login
    (LOGIN_MISSING) - an unattended agent would otherwise wait in its own
    login flow."""
    binary = _agent_binary_check(run, harness)
    if not binary.ok:
        raise PreflightError(
            binary.detail + ".", error_code="HARNESS_MISSING", hint=binary.advisory
        )
    if login:
        check = _agent_login_check(harness)
        if not check.ok:
            raise PreflightError(
                f"No {harness.name} login for an unattended launch: {check.detail}.",
                error_code="LOGIN_MISSING",
                hint=check.advisory,
            )


def _collect_rows(repo_path: str, cfg: Config) -> list[WorktreeRow]:
    """The worktree rows shown by ``list`` and the teardown chooser: one row per
    live worktree under the root, most-recently-committed first."""
    from . import ecosystems

    base_dir = worktree_root(repo_path, cfg.root)
    rows: list[WorktreeRow] = []
    for rec in git.worktree_list(repo_path):
        wt_path = Path(rec.path)
        if not path_is_under(wt_path, base_dir):
            continue
        if same_path(wt_path, repo_path):
            continue
        health = provision.worktree_health(repo_path, wt_path)
        missing = health in ("directory_missing", "invalid_linkage")
        try:
            policy: record.WorktreeRecord | None = record.load(wt_path)
        except record.RecordError:
            policy = None
        deps: DepsFreshness
        if missing:
            subject, epoch, deps, env_present = "", 0, "unknown", False
        else:
            env_present = (wt_path / ".env").is_file()
            deps = (
                lifecycle.freshness(policy.setup, ecosystems.lockfile_hash(wt_path))
                if policy
                else "unknown"
            )
            subject, epoch = git.last_commit(wt_path)
        rows.append(
            WorktreeRow(
                name=wt_path.name,
                id=worktree_id(
                    Path(resolve.Candidate.registered(repo_path, cfg.root, rec.path, None).path)
                ),
                branch=rec.branch or "detached",
                unnamed=is_placeholder(rec.branch),
                missing=missing,
                last_commit=subject,
                commit_epoch=epoch,
                path=rec.path,
                base=policy.base if policy else "",
                isolation=policy.isolation if policy else "",
                harness=policy.harness if policy else "",
                resources=policy.resources.to_json() if policy else None,
                deps=deps,
                env="present" if env_present else "absent",
                health=health,
                setup=policy.setup.status if policy else "",
                port_base=policy.ports.base if policy and policy.ports else None,
                port_count=policy.ports.count if policy and policy.ports else None,
            )
        )

    rows.sort(key=lambda r: r["commit_epoch"], reverse=True)
    return rows


@app.command(name="list")
def list_cmd(
    repo: str = typer.Option(".", "--repo", help="Git repo. Default: current repo."),
    root: str | None = typer.Option(None, "--root", help="Worktree root dir."),
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List worktrees with live branch, isolation, activity, and freshness."""
    reporter = Reporter(silent=json_out)
    cfg = _resolve_config(
        reporter,
        isolation=None,
        harness=None,
        base=None,
        root=root,
        firewall=None,
        json_out=json_out,
    )
    repo_path = _repo_root(reporter, repo, json_out=json_out)
    _check_root(reporter, repo_path, cfg.root, json_out=json_out)

    rows = _collect_rows(repo_path, cfg)

    if json_out:
        payload = {"schemaVersion": SCHEMA_VERSION, "worktrees": rows}
        _emit_json(payload)
        return

    reporter.render_list(rows, str(worktree_root(repo_path, cfg.root)))


_PT_STYLE = {
    "wt.fail": "fg:ansired",
    "wt.ok": "fg:ansigreen",
    "wt.warn": "fg:ansiyellow",
    "wt.muted": "fg:ansibrightblack",
}


def _stdin_isatty() -> bool:
    """Indirection so tests can simulate an interactive terminal."""
    return sys.stdin.isatty()


_NON_INTERACTIVE = False


def _interactive() -> bool:
    """Whether treebox may ask the user anything: a terminal on stdin, and no
    --non-interactive. The one gate for pickers, prompts and credential
    prompts, so unattended mode holds with or without a terminal."""
    return not _NON_INTERACTIVE and _stdin_isatty()


class _PickerEntry(NamedTuple):
    """One teardown-chooser line: the resolved candidate, the pre-formatted
    name/branch/age prefix, and its 'will I lose work?' status badge."""

    candidate: resolve.Candidate
    prefix: str
    status: status.WorktreeStatus


def _branch_label(row: WorktreeRow) -> str:
    return "⚠ unnamed" if row["unnamed"] else row["branch"]


def _worktree_status(
    repo_path: str, row: WorktreeRow, provider: forge.Forge | None, default_base: str
) -> status.WorktreeStatus:
    """Tier-1 git facts for one worktree, enriched with Tier-2 PR/MR state when a
    forge provider is available. Safe to call concurrently (only reads)."""
    if row["missing"]:
        return status.WorktreeStatus(
            label="⚠ missing · registration only", style="wt.warn", safe=True
        )
    path = row["path"]
    branch: str | None = None if row["branch"] == "detached" else row["branch"]
    ab = git.ahead_behind(path)
    base = row["base"] or default_base
    current_oid = git.head_oid(path)
    merged_ancestor = git.is_merged_into(repo_path, current_oid, base) if current_oid else False
    pr = None
    if provider is not None and branch and not row["unnamed"]:
        pr = provider.pr_status(repo_path, branch)
    return status.compute(
        dirty=git.is_dirty(path),
        placeholder=row["unnamed"],
        has_upstream=git.upstream_of(path) is not None,
        ahead=ab.ahead if ab else 0,
        merged_ancestor=merged_ancestor,
        pr=pr,
        current_oid=current_oid,
    )


def _compute_statuses(
    repo_path: str, rows: list[WorktreeRow], provider: forge.Forge | None, default_base: str
) -> list[status.WorktreeStatus]:
    """Status for every row, concurrently — Tier-2 forge calls do network I/O, so
    a thread pool keeps the picker responsive across many worktrees."""
    if not rows:
        return []
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
        return list(
            pool.map(lambda r: _worktree_status(repo_path, r, provider, default_base), rows)
        )


def _choose_worktrees(
    reporter: Reporter, cfg: Config, repo_path: str, *, delete_branch: bool
) -> tuple[list[resolve.Candidate], set[str]]:
    """Interactive multi-select over the live worktrees, annotated with a
    'will I lose work?' badge. Returns ``(chosen, branch_delete_paths)`` — the
    chosen candidates ([] if none exist or the user cancelled) and the subset of
    their paths whose local branch should also go. Only reached on an interactive
    TTY (never under --json)."""
    rows = _collect_rows(repo_path, cfg)
    if not rows:
        reporter.info("No worktrees to remove.")
        return [], set()

    with contextlib.suppress(git.GitError):
        git.fetch_origin(repo_path, required=False)
    provider = forge.detect(repo_path)
    statuses = _compute_statuses(repo_path, rows, provider, cfg.base)

    name_w = max(len(r["name"]) for r in rows)
    branch_w = min(max(len(_branch_label(r)) for r in rows), 30)

    entries: list[_PickerEntry] = []
    for row, st in zip(rows, statuses, strict=True):
        cand = resolve.Candidate.registered(
            repo_path,
            cfg.root,
            row["path"],
            None if row["branch"] == "detached" else row["branch"],
        )
        age = format_age(time.time() - row["commit_epoch"]) if row["commit_epoch"] else "-"
        label = _branch_label(row)
        prefix = f"{row['name']:<{name_w}}  {label:<{branch_w}.{branch_w}}  {age:>4}"
        entries.append(_PickerEntry(cand, prefix, st))

    chosen = _prompt_selection(reporter, entries)
    if not chosen:
        return [], set()
    branch_paths = _choose_branches_to_delete(reporter, entries, chosen, default=delete_branch)
    if branch_paths is None:
        reporter.info("Cancelled — nothing removed.")
        return [], set()
    return chosen, branch_paths


def _choose_branches_to_delete(
    reporter: Reporter,
    entries: list[_PickerEntry],
    chosen: list[resolve.Candidate],
    *,
    default: bool,
) -> set[str] | None:
    """After picking what to tear down, pick *which* of those also lose their
    local branch — checked ones get ``git branch -D``. Per-worktree, so keeping
    one branch while dropping another is a single pass. Returns the chosen paths
    (empty when enter is pressed with none checked), or ``None`` when the user
    cancelled (Ctrl+C) — which must abort the teardown, not proceed."""
    picked = {c.path for c in chosen}
    subset = [e for e in entries if e.candidate.path in picked]
    try:
        import questionary
    except ImportError:
        return {c.path for c in chosen} if default else set()

    choices: list[questionary.Choice] = [questionary.Separator(" ")]
    for cand, prefix, _st in subset:
        choices.append(questionary.Choice(title=prefix, value=cand.path, checked=default))
    reporter.console.print()
    answer = questionary.checkbox(
        "Also delete the local branch for … (space to pick, enter to skip)",
        choices=choices,
    ).ask()
    if answer is None:
        return None
    return set(answer)


def _prompt_selection(
    reporter: Reporter,
    entries: list[_PickerEntry],
) -> list[resolve.Candidate]:
    """Arrow-key checkbox picker via questionary; a numbered-prompt fallback keeps
    teardown working if the optional TUI dependency is missing."""
    try:
        import questionary
    except ImportError:
        return _prompt_numbered(reporter, entries)

    choices: list[questionary.Choice] = [questionary.Separator(" ")]
    for cand, prefix, st in entries:
        title: str | list[tuple[str, str]] = (
            [("", prefix + "   "), (_PT_STYLE.get(st.style, ""), st.label)] if st.label else prefix
        )
        choices.append(questionary.Choice(title=title, value=cand))
    reporter.console.print()
    answer = questionary.checkbox("Select worktrees to tear down", choices=choices).ask()
    chosen: list[resolve.Candidate] = answer or []
    return chosen


def _prompt_numbered(
    reporter: Reporter,
    entries: list[_PickerEntry],
) -> list[resolve.Candidate]:
    console = reporter.console
    console.print()
    console.print(Text("  Select worktrees to tear down:", style="wt.label"))
    for i, (_cand, prefix, st) in enumerate(entries, 1):
        line = Text(f"   {i:>2}  ")
        line.append(prefix + "   ")
        line.append(st.label, style=st.style)
        console.print(line)
    console.print()
    raw = typer.prompt(
        "  Enter numbers (e.g. 1,3 or 1-3 or all), blank to cancel",
        default="",
        show_default=False,
    )
    return [entries[i].candidate for i in _parse_selection(raw, len(entries))]


def _parse_selection(raw: str, n: int) -> list[int]:
    """Parse '1,3-5' / 'all' / '' into sorted 0-based indices within [0, n)."""
    raw = raw.strip().lower()
    if not raw:
        return []
    if raw in ("all", "*"):
        return list(range(n))
    picked: set[int] = set()
    for tok in re.split(r"[,\s]+", raw):
        if not tok:
            continue
        if "-" in tok:
            lo_s, _, hi_s = tok.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                continue
            lo, hi = max(lo, 1), min(hi, n)
            picked.update(k - 1 for k in range(lo, hi + 1))
        else:
            try:
                k = int(tok)
            except ValueError:
                continue
            if 1 <= k <= n:
                picked.add(k - 1)
    return sorted(picked)


@app.command()
def teardown(
    refs: list[str] | None = typer.Argument(
        None,
        help="Worktrees to remove: name, branch, or a unique substring of either; "
        "an absolute path matches only the worktree registered there. "
        "Omit to pick interactively from a status-annotated list.",
    ),
    repo: str = typer.Option(".", "--repo", help="Git repo. Default: current repo."),
    root: str | None = typer.Option(None, "--root", help="Worktree root dir."),
    delete_branch: bool = typer.Option(
        False, "--delete-branch", help="Also delete the local branch."
    ),
    remove_volumes: bool = typer.Option(
        False, "--remove-volumes", help="Also remove treebox volumes."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Remove even with uncommitted changes, and without the confirmation "
        "prompt. Never removes a worktree locked with `git worktree lock`.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Answer the confirmation prompt with yes, for unattended use. Only "
        "the prompt: a worktree with uncommitted changes is still refused "
        "(DIRTY_WORKTREE, exit 5); add --force to remove it anyway.",
    ),
    skip_container: bool = typer.Option(
        False, "--skip-container", help="Do not touch containers/images."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print a JSON record of what was removed."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Stream raw command output (to stderr)."
    ),
) -> None:
    """Remove worktree directories (and optionally their branches); caches are left intact.

    An incomplete cleanup fails teardown (exit 1, TEARDOWN_INCOMPLETE): it
    names what remains (for example a container docker could not remove) and
    keeps the worktree, its record and its port block, so the same command
    retries. A complete teardown releases the port block. A worktree locked
    with `git worktree lock` is never removed.

    Unattended (--json, --non-interactive, or no terminal) there is no
    prompt: pass --yes to confirm. --yes keeps the uncommitted-changes
    check (DIRTY_WORKTREE, exit 5); --force removes those too.
    """
    reporter = Reporter(quiet=quiet, verbose=verbose, silent=json_out)
    cfg = _resolve_config(
        reporter,
        isolation=None,
        harness=None,
        base=None,
        root=root,
        firewall=None,
        json_out=json_out,
    )
    repo_path = _repo_root(reporter, repo, json_out=json_out)
    _check_root(reporter, repo_path, cfg.root, json_out=json_out)

    targets: list[resolve.Candidate] = []
    branch_delete: set[str] = set()
    from_chooser = False
    if not refs:
        if json_out or not _interactive():
            raise _die(
                reporter,
                "Refusing to choose a worktree non-interactively.",
                code=EXIT_CONFLICT,
                error_code="NEEDS_CONFIRMATION",
                hint="Pass worktree name(s) to remove (treebox list shows them).",
                json_out=json_out,
            )
        targets, branch_delete = _choose_worktrees(
            reporter, cfg, repo_path, delete_branch=delete_branch
        )
        if not targets:
            return
        from_chooser = True
    else:
        seen: set[str] = set()
        for ref in refs:
            try:
                cand = resolve.resolve_ref(repo_path, cfg.root, ref)
            except resolve.AmbiguousRefError as exc:
                raise _handle(reporter, exc, json_out=json_out) from exc
            except provision.NotFoundError as exc:
                if git.local_branch_exists(repo_path, ref):
                    gone = worktree_path(repo_path, cfg.root, derive_name(ref))
                    cand = resolve.Candidate(name=derive_name(ref), branch=ref, path=str(gone))
                else:
                    raise _handle(reporter, exc, json_out=json_out) from exc
            if cand.path not in seen:
                seen.add(cand.path)
                targets.append(cand)
        if delete_branch:
            branch_delete = {c.path for c in targets}

    for cand in targets:
        if same_path(cand.path, repo_path):
            raise _die(
                reporter,
                f"Refusing to remove the main working tree '{cand.name}'.",
                code=EXIT_CONFLICT,
                error_code="MAIN_WORKTREE",
                hint="treebox only manages linked worktrees; the repo itself is never a target.",
                path=cand.path,
                json_out=json_out,
            )

    def _gate(cands: list[resolve.Candidate]) -> list[resolve.Candidate]:
        blocked = [(c, why) for c in cands if (why := _teardown_block(repo_path, c, force))]
        if blocked and not from_chooser:
            cand, why = blocked[0]
            if why == "dirty":
                raise _die(
                    reporter,
                    f"Worktree '{cand.name}' has uncommitted changes.",
                    code=EXIT_CONFLICT,
                    error_code="DIRTY_WORKTREE",
                    hint="Commit/stash the changes, or pass --force to remove anyway.",
                    path=cand.path,
                    json_out=json_out,
                )
            raise _die(
                reporter,
                f"Worktree '{cand.name}' is locked by git (git worktree lock).",
                code=EXIT_CONFLICT,
                error_code="WORKTREE_LOCKED",
                hint=f"treebox never removes a locked worktree. Unlock it first: "
                f"git -C {path_ref(repo_path)} worktree unlock {path_ref(cand.path)}",
                path=cand.path,
                json_out=json_out,
            )
        skipped.extend(blocked)
        out = {c.path for c, _ in blocked}
        return [c for c in cands if c.path not in out]

    skipped: list[tuple[resolve.Candidate, TeardownBlock]] = []
    targets = _gate(targets)

    confirmed = force or yes or from_chooser
    if not confirmed and any(Path(c.path).is_dir() for c in targets):
        if _interactive() and not json_out:
            listed = ", ".join(c.name for c in targets)
            plural = "s" if len(targets) != 1 else ""
            typer.confirm(f"Remove worktree{plural} {listed}?", abort=True)
        else:
            raise _die(
                reporter,
                "Refusing to remove non-interactively without confirmation.",
                code=EXIT_CONFLICT,
                error_code="NEEDS_CONFIRMATION",
                hint="Pass --yes to confirm (uncommitted changes still block the removal), "
                "or --force to also remove uncommitted changes.",
                json_out=json_out,
            )

    records: list[TeardownRecord] = []
    if targets:
        try:
            with contextlib.ExitStack() as stack:
                for cand in targets:
                    stack.enter_context(locking.worktree_lock(Path(cand.path)))
                targets = _gate(targets)
                runners: list[Runner | None] = (
                    [None] * len(targets)
                    if skip_container
                    else [
                        _teardown_runner(cfg, cand, remove_volumes=remove_volumes)
                        for cand in targets
                    ]
                )
                if targets:
                    reporter.heading("teardown", ", ".join(c.name for c in targets))
                records = [
                    _teardown_one(
                        reporter,
                        cfg,
                        cand,
                        repo_path,
                        run=run,
                        delete_branch=cand.path in branch_delete,
                        force=force,
                        skip_container=skip_container,
                        json_out=json_out,
                    )
                    for cand, run in zip(targets, runners, strict=True)
                ]
        except locking.LockError as exc:
            raise _handle(reporter, exc, json_out=json_out) from exc

    if skipped:
        _report_skipped(reporter, skipped, removed=sum(r["complete"] for r in records))

    if json_out:
        _emit_json(
            {"schemaVersion": SCHEMA_VERSION, "worktrees": records, "warnings": reporter.warnings}
        )

    incomplete = [r for r in records if not r["complete"]]
    if incomplete:
        raise _die(
            reporter,
            "Teardown is incomplete: "
            + "; ".join(f"{r['name']}: {', '.join(r['remaining'])}" for r in incomplete)
            + ".",
            code=EXIT_ERROR,
            error_code="TEARDOWN_INCOMPLETE",
            hint="The worktree and its ownership record are kept. Fix the cause, then run "
            "the same teardown again.",
            path=incomplete[0]["worktree_path"],
            json_out=json_out,
        )

    if skipped:
        raise typer.Exit(EXIT_CONFLICT)


TeardownBlock = Literal["dirty", "locked"]


def _teardown_block(repo_path: str, cand: resolve.Candidate, force: bool) -> TeardownBlock | None:
    """Whether teardown must keep ``cand``, and why. A worktree git cannot
    use (missing directory, invalid linkage) is never asked about dirtiness:
    git there would walk up into the main checkout."""
    path = Path(cand.path)
    if any(rec.locked and same_path(rec.path, path) for rec in git.worktree_list(repo_path)):
        return "locked"
    if force or provision.worktree_health(repo_path, path) not in ("healthy", "unreadable_state"):
        return None
    return "dirty" if git.is_dirty(path) else None


def _report_skipped(
    reporter: Reporter, skipped: list[tuple[resolve.Candidate, TeardownBlock]], *, removed: int
) -> None:
    """Make the mixed-selection outcome unmistakable: name each worktree we kept
    and why, how to remove it anyway, and a one-line tally so the user knows
    exactly what did and didn't happen."""
    reporter.blank()
    for cand, why in skipped:
        reporter.fail(
            cand.name, "kept · uncommitted changes" if why == "dirty" else "kept · locked"
        )
    if any(why == "dirty" for _, why in skipped):
        reporter.hint("commit or stash the changes, or re-run teardown with --force")
    if any(why == "locked" for _, why in skipped):
        reporter.hint("unlock it first (git worktree unlock <path>); treebox never removes it")
    reporter.blank()
    plural = "s" if removed != 1 else ""
    reasons = " or ".join(
        sorted({"with uncommitted changes" if why == "dirty" else "locked" for _, why in skipped})
    )
    reporter.info(f"Removed {removed} worktree{plural} · kept {len(skipped)} {reasons}.")


ContainerOutcome = Literal["cleaned", "skipped", "failed"]


class TeardownRecord(TypedDict):
    """One worktree's teardown result — emitted verbatim in the ``--json``
    ``worktrees`` array (porcelain discipline applies)."""

    name: str
    branch: str | None
    worktree_path: str
    removed: bool
    branch_deleted: bool
    container: ContainerOutcome
    volumes_removed: bool
    complete: bool
    remaining: list[str]


def _teardown_runner(cfg: Config, cand: resolve.Candidate, *, remove_volumes: bool) -> Runner:
    """Resolve one teardown target's runner from host-owned files only.

    The host-owned record names the isolation mode. It is keyed by the
    registered path and lives outside the worktree, so it survives a corrupt
    tree (missing ``.git`` pointer) and even a deleted worktree directory.

    Teardown is the recovery for a missing or invalid record (``enter``
    refuses such a worktree), so it must work without one. Then the docker
    runner's resource manifest decides: ``create`` writes it outside every
    sandbox mount before any docker resource exists, so every docker worktree
    has one and a host worktree never does. The config default and the
    worktree's sandbox-writable state are never read for this.
    ``remove_volumes`` is handed to the factory: teardown options and
    resource ownership are runner concerns, so the docker runner reads its
    authoritative host-owned manifest outside the sandbox mount."""
    path = Path(cand.path)
    try:
        isolation = record.load(path).isolation
    except record.RecordError:
        isolation = DockerRunner.name if has_resource_manifest(path) else HostRunner.name
    return get_runner(cfg.with_overrides(isolation=isolation), remove_volumes=remove_volumes)


def _teardown_one(
    reporter: Reporter,
    cfg: Config,
    cand: resolve.Candidate,
    repo_path: str,
    *,
    run: Runner | None,
    delete_branch: bool,
    force: bool,
    skip_container: bool,
    json_out: bool,
) -> TeardownRecord:
    """Tear down one resolved worktree; returns its --json record. ``run`` is
    the runner from ``_teardown_runner``, or ``None`` under
    ``--skip-container`` (no container work, so no runner was resolved)."""
    wt = Worktree.at(repo_path, Path(cand.path), branch=cand.branch or "")
    try:
        result = cleanup.cleanup_one(
            wt, run, force=force, delete_branch=delete_branch, reporter=reporter
        )
    except cleanup.MainWorktreeError as exc:
        raise _die(
            reporter,
            str(exc),
            code=EXIT_CONFLICT,
            error_code="MAIN_WORKTREE",
            hint="treebox only manages linked worktrees; the repo itself is never a target.",
            path=str(wt.path),
            json_out=json_out,
        ) from exc

    if skip_container:
        reporter.note("container", "skipped")
    if result.runner_error is not None:
        reporter.warn(f"isolation teardown: {result.runner_error}")
    shown = _short_path(wt.path, repo_path)
    if result.worktree == "removed":
        reporter.ok("worktree", f"removed {shown}")
    elif result.worktree == "already_gone":
        reporter.note("worktree", f"already gone · {shown}")
    elif result.worktree == "cleanup_incomplete":
        reporter.note("worktree", f"kept · cleanup incomplete · {shown}")
    else:
        reporter.fail("worktree", f"kept · {shown}")
    if result.port_error is not None:
        reporter.warn(
            f"port block not released now (the next create reclaims it): {result.port_error}"
        )
    elif result.released_ports is not None:
        reporter.ok("ports", f"released {result.released_ports}")
    if not result.complete:
        reporter.note("record", "kept · the same teardown retries")
    elif result.branch_error is not None:
        reporter.warn(f"could not delete branch: {result.branch_error}")
    elif result.branch_deleted:
        reporter.ok("branch", f"deleted {result.branch}")
    elif result.branch:
        reporter.note("branch", f"kept {result.branch}")

    return TeardownRecord(
        name=cand.name,
        branch=result.branch,
        worktree_path=str(wt.path),
        removed=result.worktree == "removed",
        branch_deleted=result.branch_deleted,
        container=result.runner.container,
        volumes_removed=result.runner.volumes_removed,
        complete=result.complete,
        remaining=list(result.remaining),
    )


@app.command()
def doctor(
    repo: str = typer.Option(".", "--repo", help="Git repo. Default: current repo."),
    isolation: str | None = typer.Option(
        None,
        "--isolation",
        help=f"Isolation mode to check: {'|'.join(VALID_ISOLATION)}. docker needs a non-root user.",
    ),
    harness: str | None = typer.Option(
        None,
        "--harness",
        "-H",
        help=f"Agent harness to check: {'|'.join(VALID_HARNESSES)}. Default: the "
        "config default (the harness create launches).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Check git, the agent, its login, UID/GID, and isolation-specific dependencies.

    For the selected harness (--harness, else the config default) doctor
    checks what a launch needs, where the agent runs: on the host, the
    harness binary on PATH and a real login (its login file, or on macOS
    the Keychain item the CLI keeps it in); in a docker sandbox, the login
    file the sandbox gets a copy of (the binary is part of the image and is
    checked at launch). A login dir that merely exists is no login. Those
    two rows are hard checks (exit 1), like git, repo and isolation; the
    other harnesses' logins are shown as optional.
    """
    reporter = Reporter(silent=json_out)
    cfg = _resolve_config(
        reporter,
        isolation=isolation,
        harness=harness,
        base=None,
        root=None,
        firewall=None,
        json_out=json_out,
    )
    run = get_runner(cfg)
    selected = get_harness(cfg.harness)

    checks: list[DoctorCheck] = []

    git_ok = git.have_git()
    git_ver = git.version_str() if git_ok else "missing"
    checks.append(DoctorCheck("git", git_ok, git_ver))

    repo_path = ""
    try:
        repo_path = git.main_worktree(expand_user(repo))
        checks.append(DoctorCheck("repo", True, repo_path))
    except git.GitError as exc:
        checks.append(DoctorCheck("repo", False, str(exc)))

    ident = system.identity()
    checks.append(DoctorCheck("uid/gid", True, f"{ident.uid}:{ident.gid}"))

    checks.append(_agent_binary_check(run, selected))
    for h in HARNESSES:
        checks.append(_agent_login_check(h, required=h is selected))

    env_file = (
        provision.resolve_env_file(repo_path, cfg.env_file) if repo_path else Path(cfg.env_file)
    )
    checks.append(DoctorCheck(".env", env_file.is_file(), str(env_file), required=False))

    def _check_git_auth() -> DoctorCheck:
        reachable = git.origin_reachable(repo_path) if repo_path else None
        if reachable is None:
            return DoctorCheck("git auth", True, "no remote (local-only; freshness N/A)")
        if reachable:
            return DoctorCheck("git auth", True, "authenticated · fresh fetch will succeed")
        return DoctorCheck(
            "git auth",
            False,
            "no working credential for origin",
            "git can't authenticate to origin without a prompt — `create` requires a "
            "fresh fetch. In a terminal it will prompt for your SSH key passphrase / "
            "credentials and continue; headless (no TTY) it fails (exit 4) unless you "
            "pass --no-fetch. Authenticate once to avoid prompts: `gh auth login` "
            "(GitHub), `glab auth login` (GitLab), a git credential helper / HTTPS "
            "token (others), or load an ssh-agent.",
        )

    def _check_runner() -> DoctorCheck:
        name = f"isolation: {run.name}"
        try:
            run.preflight(reporter)
        except PreflightError as exc:
            return DoctorCheck(name, False, str(exc), exc.hint)
        except RuntimeError as exc:
            return DoctorCheck(name, False, str(exc))
        return DoctorCheck(name, True, run.facts().preflight_detail)

    slow: list[tuple[str, Callable[[], DoctorCheck]]] = [
        ("checking git auth", _check_git_auth),
        ("checking isolation", _check_runner),
    ]
    if json_out:
        checks.extend(check() for _, check in slow)
        problems = _doctor_problems(checks, selected)
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "ok": not problems,
            "isolation": cfg.isolation,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            "advisories": _doctor_advisories(checks),
        }
        _emit_json(payload)
        if problems:
            raise typer.Exit(1)
        return

    names_ = [c.name for c in checks] + ["git auth", f"isolation: {run.name}"]
    width = max(len(name) for name in names_)

    checks, _ = reporter.render_doctor(checks, slow, cfg.isolation, width)
    problems = _doctor_problems(checks, selected)
    reporter.render_doctor_verdict(problems=problems, advisories=_doctor_advisories(checks))
    if problems:
        raise typer.Exit(1)


def _doctor_problems(checks: list[DoctorCheck], selected: Harness) -> list[str]:
    """The hard-check failures (git, repo, the selected harness's binary and
    login, isolation) that make doctor exit 1 - one definition so the human
    and --json paths share an exit-code contract."""
    hard = {"git", "repo", f"agent: {selected.name}", f"login: {selected.name}"}
    return [
        c.name for c in checks if not c.ok and (c.name in hard or c.name.startswith("isolation"))
    ]


def _doctor_advisories(checks: list[DoctorCheck]) -> list[str]:
    """How to fix each failing check that matters (an optional row's
    advisory, such as another harness's login, is not shown)."""
    return [c.advisory for c in checks if c.advisory and not c.ok and c.required]


def _resolve_version() -> str:
    """The installed distribution version, falling back to the package constant."""
    try:
        from importlib.metadata import version as _dist_version

        return _dist_version("treebox")
    except Exception:
        return __version__


@app.command()
def version() -> None:
    """Print the version."""
    sys.stdout.write(f"{_resolve_version()}\n")


template_app = typer.Typer(
    no_args_is_help=True,
    help="Scaffold and inspect operator-owned sandbox templates (docker "
    "isolation). Named templates live in $TREEBOX_HOME/templates/<name>.",
)
app.add_typer(template_app, name="template")


def _template_source(name: str) -> str:
    """Where ``template_dir(name)`` resolves from, for display: an explicit
    ``$TREEBOX_TEMPLATE_DIR`` wins for any name, else a user dir by name, else
    the bundled default."""
    if os.environ.get("TREEBOX_TEMPLATE_DIR"):
        return "env"
    if (assets.user_templates_root() / name).is_dir():
        return "user"
    return "bundled"


@template_app.command("init")
def template_init(
    name: str = typer.Argument(
        ..., help="Name for the new template (a directory under $TREEBOX_HOME/templates)."
    ),
    from_template: str = typer.Option(
        assets.DEFAULT_TEMPLATE, "--from", help="Existing template to copy from."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing template of this name."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print a machine-readable JSON result."),
) -> None:
    """Copy a template into $TREEBOX_HOME/templates/<name> so you can edit it.

    This is the sanctioned replacement for hand-copying the shipped directory:
    it works from any install. Then edit the Dockerfile and container.json and run
    `treebox create <name> --isolation docker --template <name>`.
    """
    reporter = Reporter(silent=json_out)
    if not is_valid_template_name(name):
        raise _die(
            reporter,
            f"Invalid template name '{name}'. Use letters, digits, '.', '_', or '-'.",
            code=EXIT_USAGE,
            error_code="INVALID_NAME",
            json_out=json_out,
        )
    if name == assets.STAGING_DIR:
        raise _die(
            reporter,
            f"Invalid template name '{name}': treebox reserves it for its staging directory.",
            code=EXIT_USAGE,
            error_code="INVALID_NAME",
            json_out=json_out,
        )
    if from_template not in assets.available_templates():
        raise _die(
            reporter,
            f"No template named '{from_template}' to copy from. "
            f"Run 'treebox template list' to see available templates.",
            code=EXIT_NOTFOUND,
            error_code="TEMPLATE_NOT_FOUND",
            json_out=json_out,
        )
    try:
        src = assets.template_dir(from_template)
    except assets.TemplateNotFoundError as exc:
        raise _die(
            reporter,
            str(exc),
            code=EXIT_NOTFOUND,
            error_code="TEMPLATE_NOT_FOUND",
            json_out=json_out,
        ) from exc

    dest = assets.user_templates_root() / name
    if src.resolve() == dest.resolve():
        raise _die(
            reporter,
            f"Source and destination are the same template ({dest}); nothing to copy.",
            code=EXIT_USAGE,
            error_code="TEMPLATE_CONFLICT",
            json_out=json_out,
        )
    existing = dest.is_symlink() or dest.exists()
    if existing and not force:
        raise _die(
            reporter,
            f"Template '{name}' already exists at {dest}.",
            code=EXIT_CONFLICT,
            error_code="TEMPLATE_EXISTS",
            hint="Pass --force to overwrite, or choose another name.",
            path=str(dest),
            json_out=json_out,
        )

    def _remove(target: Path) -> None:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.is_symlink() or target.exists():
            target.unlink()

    staging_root = dest.parent / assets.STAGING_DIR
    staging: Path | None = None
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{name}.", dir=staging_root))
        shutil.copytree(src, staging / "new")
    except OSError as exc:
        if staging is not None:
            with contextlib.suppress(OSError):
                _remove(staging)
        raise _die(
            reporter,
            f"Could not copy template into {dest}: {exc}.",
            code=EXIT_ERROR,
            error_code="COPY_FAILED",
            path=str(dest),
            json_out=json_out,
        ) from exc

    old = staging / "old"
    if existing:
        try:
            dest.rename(old)
        except OSError as exc:
            with contextlib.suppress(OSError):
                _remove(staging)
            raise _die(
                reporter,
                f"Could not overwrite existing template at {dest}: {exc}. "
                "The existing template is unchanged.",
                code=EXIT_ERROR,
                error_code="OVERWRITE_FAILED",
                path=str(dest),
                json_out=json_out,
            ) from exc

    try:
        (staging / "new").rename(dest)
    except OSError as exc:
        kept = ""
        if existing:
            try:
                old.rename(dest)
            except OSError:
                kept = f" The previous template is kept at {old}."
        if not kept:
            with contextlib.suppress(OSError):
                _remove(staging)
        raise _die(
            reporter,
            f"Could not copy template into {dest}: {exc}.{kept}",
            code=EXIT_ERROR,
            error_code="COPY_FAILED",
            path=str(dest),
            json_out=json_out,
        ) from exc

    leftover = ""
    try:
        _remove(staging)
    except OSError as exc:
        leftover = (
            f"could not remove the previous template copy at {staging} ({exc}); delete it by hand"
        )

    missing = assets.missing_template_files(dest)
    if json_out:
        _emit_json(
            {
                "schemaVersion": SCHEMA_VERSION,
                "template": {
                    "name": name,
                    "path": str(dest),
                    "from": from_template,
                    "valid": not missing,
                    "missing": missing,
                },
            }
        )
        return
    reporter.ok("template", f"{name} created at {dest}")
    if missing:
        reporter.warn(
            f"copied from an incomplete source — missing required files: {', '.join(missing)}"
        )
    if leftover:
        reporter.warn(leftover)
    reporter.hint(
        "Edit the Dockerfile and container.json, then: "
        f"treebox create <name> --isolation docker --template {name}"
    )


@template_app.command("list")
def template_list(
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List selectable templates, their required-file status, and the default."""
    reporter = Reporter(silent=json_out)
    cfg = _resolve_config(
        reporter,
        isolation=None,
        harness=None,
        base=None,
        root=None,
        firewall=None,
        json_out=json_out,
    )
    override = os.environ.get("TREEBOX_TEMPLATE_DIR")
    rows: list[TemplateRow] = []
    for name in assets.available_templates():
        try:
            path = assets.template_dir(name)
        except assets.TemplateNotFoundError:
            continue
        missing = assets.missing_template_files(path)
        invalid = assets.invalid_template_json_files(path)
        rows.append(
            TemplateRow(
                name=name,
                path=str(path),
                source=_template_source(name),
                default=name == cfg.template,
                valid=not missing and not invalid,
                missing=missing,
                invalid=invalid,
                firewall=(path / assets.FIREWALL_FILE).is_file()
                and assets.FIREWALL_FILE not in invalid,
            )
        )

    if json_out:
        _emit_json(
            {
                "schemaVersion": SCHEMA_VERSION,
                "templateDirOverride": override,
                "templates": rows,
            }
        )
        return

    if override:
        reporter.warn(f"$TREEBOX_TEMPLATE_DIR overrides every template → {override}")
    show_highlights = any(
        r["name"] == assets.DEFAULT_TEMPLATE and r["source"] == "bundled" for r in rows
    )
    reporter.render_template_list(
        rows,
        highlights=assets.DEFAULT_TEMPLATE_HIGHLIGHTS if show_highlights else (),
    )


template_app.command(name="ls", hidden=True)(template_list)


@template_app.command("path")
def template_path(
    name: str = typer.Argument(assets.DEFAULT_TEMPLATE, help="Template name (default: 'default')."),
) -> None:
    """Print the resolved directory for a template.

    For scripting — e.g. `cd "$(treebox template path react)"`. This is the
    install-agnostic answer to "where does this template live", including the
    bundled default's location.
    """
    reporter = Reporter()
    try:
        path = assets.template_dir(name)
    except assets.TemplateNameError as exc:
        raise _die(reporter, str(exc), code=EXIT_USAGE, error_code="INVALID_NAME") from exc
    except assets.TemplateNotFoundError as exc:
        raise _die(reporter, str(exc), code=EXIT_NOTFOUND, error_code="TEMPLATE_NOT_FOUND") from exc
    sys.stdout.write(f"{path}\n")


def _version_callback(value: bool) -> None:
    if value:
        sys.stdout.write(f"{_resolve_version()}\n")
        raise typer.Exit()


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        envvar="TREEBOX_NON_INTERACTIVE",
        help="Unattended mode, also with a terminal attached: treebox never "
        "opens a picker, asks a question, prompts for git or ssh credentials, "
        "or waits on the terminal. What would need an answer fails with a "
        "classified error instead (teardown without --yes or --force is "
        "NEEDS_CONFIRMATION; a fetch that needs credentials is FETCH_FAILED), "
        "and a launch without a usable agent login is LOGIN_MISSING. Give it "
        "before the command: treebox --non-interactive create NAME. "
        "Without a terminal on stdin, treebox already behaves this way.",
    ),
) -> None:
    global _NON_INTERACTIVE
    _NON_INTERACTIVE = non_interactive
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    from rich.traceback import install

    install(show_locals=False, suppress=[typer], width=100)


app.command(name="ls", hidden=True)(list_cmd)
app.command(name="rm", hidden=True)(teardown)


def run() -> None:
    """The ``treebox`` console script: the app, with no trailing whitespace
    on any line off a terminal (Typer's help is printed before ``main``)."""
    trim_line_ends()
    app()


if __name__ == "__main__":
    run()
