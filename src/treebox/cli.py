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
    provision,
    record,
    resolve,
    status,
    system,
)
from .config import Config, load_config, validate_config
from .harnesses import HARNESSES, VALID_HARNESSES, Harness, get_harness
from .models import (
    PLACEHOLDER_PREFIX,
    DepsFreshness,
    TemplateRow,
    Worktree,
    WorktreeRow,
    derive_name,
    expand_user,
    flatten_branch,
    is_placeholder,
    is_valid_name,
    is_valid_template_name,
    path_is_under,
    placeholder_branch,
    same_path,
    worktree_path,
    worktree_root,
)
from .output import DoctorCheck, Reporter, StepError, format_age, format_elapsed
from .runners import VALID_ISOLATION, DockerRunner, HostRunner, PreflightError, Runner, get_runner
from .runners.docker import has_resource_manifest

# Stable, documented exit codes (see the epilog on `--help`). Agents branch on these.
EXIT_OK = 0
EXIT_ERROR = 1  # generic / unexpected failure
EXIT_USAGE = 2  # bad invocation (invalid name/branch, ambiguous ref, bad option)
EXIT_NOTFOUND = 3  # the worktree/branch the command needs does not exist
EXIT_PERMISSION = 4
EXIT_CONFLICT = 5  # already exists / uncommitted changes / lock held

# Current JSON schema version. Payloads only ever *gain* fields within a
# version (git-porcelain discipline); a breaking reshape or rename bumps it.
# Agents branch on these payloads, so treat the shape as a contract.
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


# --- shared helpers ----------------------------------------------------------


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
        # A malformed/invalid user config.toml is a usage problem, not a crash:
        # exit 2 with a styled message — or a structured error in --json mode.
        # TypeError is the belt-and-braces guard: load_config validates types
        # and raises ValueError, but no config typo may ever escape as a
        # traceback again (issue #139).
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
        # Re-check after flag overrides: load_config only vets the file, and a
        # bogus --isolation/--harness must be a usage error, not a traceback.
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
    cfg = cfg.with_overrides(isolation=rec.isolation, firewall=rec.firewall)
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
        f"Tear it down and create it again: treebox teardown {exc.worktree.name}, "
        "then treebox create (add --checkout <branch> to keep its branch)."
    )


def _repo_root(reporter: Reporter, repo: str, *, json_out: bool = False) -> str:
    try:
        # Anchor to the *main* worktree so every command sees the same worktree
        # set and root whether invoked from the repo, from .treebox/, or from
        # inside a linked worktree (where --show-toplevel would mislead us).
        return git.main_worktree(expand_user(repo))
    except git.GitMissingError as exc:
        # Not a repo problem: git itself can't run. Distinct stable code so
        # agents don't chase the misleading "run inside a git repo" hint.
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


# Exceptions provisioning can raise, mapped to exit codes by _handle().
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
        # Before FetchError/GitError branches: the binary itself is absent, so
        # auth/repo hints would mislead.
        return _ErrorInfo(EXIT_ERROR, "GIT_MISSING", _GIT_MISSING_HINT)
    if isinstance(exc, locking.LockError):
        return _ErrorInfo(
            EXIT_CONFLICT,
            "LOCK_HELD",
            "Another run holds this worktree — wait, then retry.",
        )
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
        # Same classification the template sub-app gives this condition: a
        # missing template is not-found everywhere, never the generic catch-all.
        return _ErrorInfo(
            EXIT_NOTFOUND,
            "TEMPLATE_NOT_FOUND",
            "Run 'treebox template list' to see available templates, or scaffold "
            f"this one: treebox template init {exc.name}",
        )
    if isinstance(exc, assets.TemplateInvalidError):
        # The template exists but its contents can't serve this run - a runtime
        # error (exit 1) with a stable code, like PreflightError below; agents
        # branch on error.code.
        return _ErrorInfo(
            EXIT_ERROR,
            "TEMPLATE_INVALID",
            f"Inspect the template dir: treebox template path {exc.name}",
        )
    if isinstance(exc, PreflightError):
        # Runner dependency problems keep exit 1 (codes are stable; agents
        # branch on error.code instead: MISSING_DEPENDENCY, DOCKER_UNAVAILABLE,
        # RUNNING_AS_ROOT).
        return _ErrorInfo(EXIT_ERROR, exc.error_code, exc.hint)
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
        if isinstance(exc, (provision.ProvisionError, record.RecordError)) and exc.path
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
    outcome: provision.Outcome, *, json_out: bool, print_only: bool, warnings: list[str]
) -> None:
    """Write the machine/script-facing result to stdout. ``warnings`` are the
    run's warnings (an additive payload field: empty when nothing was skipped)."""
    if json_out:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "name": outcome.worktree.name,
            "worktree_path": str(outcome.worktree.path),
            "branch": outcome.worktree.branch,
            "base": outcome.worktree.base,
            "entry_command": outcome.entry_command,
            "created": outcome.created,
            "warnings": warnings,
        }
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
                "warnings": reporter.warnings,
            }
        )
        return

    reporter.heading("create", f"{name}  ·  dry run")
    reporter.summary("worktree", _short_path(wt.path, repo_path))
    reporter.summary("branch", branch)
    reporter.summary("isolation", f"{cfg.isolation}  →  {cfg.harness}")
    reporter.blank()
    for cmd in cmds:
        reporter.command(cmd)
    reporter.blank()


# --- create ------------------------------------------------------------------


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
        # Tri-state: an explicit --firewall/--no-firewall overrides the config
        # in either direction; unset (None) falls through to the config default,
        # like every other config-backed option.
        firewall=firewall,
        template=template,
        json_out=json_out,
    )
    if name is not None and name.startswith("-"):
        # A dash-led NAME is never a valid branch (INVALID_BRANCH, as
        # always), and it usually means `create -- ARGS`: the first agent arg
        # took the name slot. Say how to pass agent args.
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

    if checkout is not None:
        wt_name = flatten_branch(name) if name else derive_name(checkout)
        target_branch = checkout
    elif name is not None:
        # An explicit name IS the branch: slashes stay in the branch, the
        # directory flattens them (feature/auth -> feature--auth).
        wt_name = flatten_branch(name)
        target_branch = name
        _validate_branch(reporter, target_branch, json_out=json_out)
    else:
        wt_name = names.petname(lambda n: _name_taken(repo_path, cfg.root, n))
        target_branch = placeholder_branch(wt_name)

    run = get_runner(cfg)

    # Resolve the template - and parse the JSON it would render - before any
    # git state exists: failing at container render time would strand a freshly
    # provisioned worktree + branch (a retried create then hits BRANCH_EXISTS).
    # create-only on purpose - enter must keep tolerating a deleted recorded
    # template on an existing container.
    if cfg.isolation == "docker":
        try:
            assets.load_template_json(cfg.template, assets.CONFIG_FILE)
            if cfg.firewall:
                assets.load_template_json(cfg.template, assets.FIREWALL_FILE)
        except (assets.TemplateNotFoundError, assets.TemplateInvalidError) as exc:
            raise _handle(reporter, exc, json_out=json_out) from exc

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
    if cold:
        reporter.summary("cache", "cold (from source)")
    reporter.blank()

    # The boundary str was validated by _resolve_config; internal seams take
    # the resolved object.
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
            # Prompt for git credentials only when treebox may ask (a
            # terminal, no --non-interactive) - like `git pull`. git and ssh
            # ask on the terminal itself, out of the way of --json's stdout.
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
    )


# --- enter -------------------------------------------------------------------


@app.command()
def enter(
    ref: str = typer.Argument(..., help="Worktree name, branch, or a unique substring of either."),
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
    repo_path = _repo_root(reporter, repo, json_out=json_out)
    try:
        cand = resolve.resolve_ref(repo_path, cfg.root, ref)
    except provision.NotFoundError as exc:
        # The branch may exist without a worktree (torn down, or never had
        # one): point at the command that materializes it instead of the
        # generic miss advice.
        if git.local_branch_exists(repo_path, ref) or git.remote_branch_exists(repo_path, ref):
            exc.hint = (
                f"Branch '{ref}' exists but has no worktree — "
                f"run `treebox create --checkout {ref}`."
            )
        raise _handle(reporter, exc, json_out=json_out) from exc
    except _PROVISION_ERRORS as exc:
        raise _handle(reporter, exc, json_out=json_out) from exc
    # The creation policy comes only from the host-owned record. A missing or
    # invalid one is a classified error with a recovery path, never a fallback
    # to the config default (or to the worktree's sandbox-writable state).
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
    # Names are final after reconciliation: resolve the objects once.
    run = get_runner(cfg)
    agent = get_harness(cfg.harness)

    subtitle = cand.name if cand.branch in (None, cand.name) else f"{cand.name}  ·  {cand.branch}"
    reporter.heading("enter", subtitle)
    reporter.summary("isolation", f"{cfg.isolation}  →  {cfg.harness}")
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
) -> None:
    """The session spine shared by ``create`` and ``enter``: runner preflight,
    the lock on the worktree at ``path``, the provisioning step, one exception
    → exit-code classification, then the ``--json`` / ``--print`` / launch fork.

    Preflight runs first so runner-specific preconditions (a no-op for the
    host runner) fail fast with a clean error + hint — before any git state
    is touched, and never surfacing a stopped docker daemon deep inside
    launch as a misleading "no container" that steers users into a
    destructive teardown. A launch also checks the agent first (the harness
    binary on the host; in unattended mode also its login), so a missing
    prerequisite fails before anything is provisioned.

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
        if launching:
            _require_agent(run, harness, login=not _interactive())
        with locking.worktree_lock(path):
            outcome = provision_call()
            if json_out or print_only:
                # The launch path makes the sandbox entry-ready inside
                # run.launch; the emit path must do the same, or a stopped
                # container yields an entry_command that is dead on replay
                # (and a manual `docker start` workaround would silently
                # skip the firewall re-init).
                run.prepare_entry(outcome.worktree)
    except _PROVISION_ERRORS as exc:
        raise _handle(reporter, exc, json_out=json_out) from exc

    if not launching:
        _emit_result(outcome, json_out=json_out, print_only=print_only, warnings=reporter.warnings)
        return
    reporter.blank()
    # Style-A closing line: green "Ready", dim "in <total> — launching <harness>".
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
            f"{outcome.worktree.name}",
        ) from exc
    except RuntimeError as exc:
        raise _handle(reporter, exc, json_out=False) from exc
    if code == 0:
        raise typer.Exit(0)
    # A negative code: the agent was killed by that signal (shell convention).
    status = code if code > 0 else 128 - code
    killed = f" (killed by {system.signal_name(-code)})" if code < 0 else ""
    sys.stderr.write(f"{AGENT_EXIT_PREFIX}{status}{killed}\n")
    sys.stderr.flush()
    raise typer.Exit(status)


# The fixed prefix of the one line treebox prints last, on stderr, when the
# launched agent exits non-zero: "treebox: agent exited with status <N>". A
# documented contract (docs/usage.md): a caller tells an agent exit from a
# treebox failure by it. Never printed for a treebox failure.
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


def _agent_login_check(run: Runner, harness: Harness, *, required: bool = True) -> DoctorCheck:
    """The harness's subscription login, as the agent will see it: the host
    login on the host; in a sandbox, the login file it gets a copy of."""
    name = f"login: {harness.name}"
    sandboxed = run.facts().sandboxed
    found = harness.sandbox_login() if sandboxed else harness.host_login()
    if found:
        return DoctorCheck(name, True, found, required=required)
    advisory = harness.login_hint()
    if sandboxed:
        advisory += (
            f" A {run.name} sandbox gets a copy of {harness.login_file()}, so that "
            "file must hold the login."
        )
    return DoctorCheck(
        name, False, f"no login at {harness.login_file()}", advisory, required=required
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
        check = _agent_login_check(run, harness)
        if not check.ok:
            raise PreflightError(
                f"No {harness.name} login for an unattended launch: {check.detail}.",
                error_code="LOGIN_MISSING",
                hint=check.advisory,
            )


# --- list --------------------------------------------------------------------


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
        # A root containing the repo (e.g. root = "..") makes the main checkout
        # pass the filter above; it is not a treebox worktree and must never be
        # listed (or offered by the teardown chooser).
        if same_path(wt_path, repo_path):
            continue
        # A registration whose dir is gone, or whose .git pointer no longer
        # leads to it, must never be shelled into: git would crash on the
        # missing path or act on the main checkout. `missing` keeps its old
        # meaning for those two health values so teardown can prune them.
        health = provision.worktree_health(repo_path, wt_path)
        missing = health in ("directory_missing", "invalid_linkage")
        # The policy columns come from the host-owned record; a missing or
        # invalid record shows as honestly unknown (""), never as a guess.
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
                branch=rec.branch or "detached",
                unnamed=is_placeholder(rec.branch),
                missing=missing,
                last_commit=subject,
                commit_epoch=epoch,
                path=rec.path,
                base=policy.base if policy else "",
                isolation=policy.isolation if policy else "",
                harness=policy.harness if policy else "",
                deps=deps,
                env="present" if env_present else "absent",
                health=health,
                setup=policy.setup.status if policy else "",
            )
        )

    # Most recently touched first: the worktree you're looking for is almost
    # always the one you (or an agent) just committed in.
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

    rows = _collect_rows(repo_path, cfg)

    if json_out:
        payload = {"schemaVersion": SCHEMA_VERSION, "worktrees": rows}
        _emit_json(payload)
        return

    reporter.render_list(rows, str(worktree_root(repo_path, cfg.root)))


# --- teardown ----------------------------------------------------------------

# Rich theme style -> prompt_toolkit style, so the interactive picker's badges
# carry the same green/yellow/red semantics as the rest of the CLI.
_PT_STYLE = {
    "wt.fail": "fg:ansired",
    "wt.ok": "fg:ansigreen",
    "wt.warn": "fg:ansiyellow",
    "wt.muted": "fg:ansibrightblack",
}


def _stdin_isatty() -> bool:
    """Indirection so tests can simulate an interactive terminal."""
    return sys.stdin.isatty()


# Set by the global --non-interactive option (or $TREEBOX_NON_INTERACTIVE).
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
        # The dir is gone — don't shell into it. It's a stale registration;
        # tearing it down just prunes git's record, so it's safe to remove.
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

    # Best-effort refresh so "merged" reflects the real origin/<base>, not a
    # stale local ref. Never blocks or prompts; offline just uses the last fetch.
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
    # The picker is the whole teardown decision, so the branch question lives here
    # too. It's a second multi-select, not one yes/no for the batch — deleting a
    # branch is per-worktree (keep one, drop another) rather than all-or-none.
    branch_paths = _choose_branches_to_delete(reporter, entries, chosen, default=delete_branch)
    if branch_paths is None:
        # Ctrl+C on the branch question backs out of the whole teardown — the
        # user is abandoning the decision, not answering "keep the branches".
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
        # The picker's optional dep is absent; keep every branch (the safe
        # default) and let --delete-branch cover the scripted path.
        return {c.path for c in chosen} if default else set()

    choices: list[questionary.Choice] = [questionary.Separator(" ")]
    for cand, prefix, _st in subset:
        choices.append(questionary.Choice(title=prefix, value=cand.path, checked=default))
    reporter.console.print()
    answer = questionary.checkbox(
        "Also delete the local branch for … (space to pick, enter to skip)",
        choices=choices,
    ).ask()
    # .ask() maps Ctrl+C to None; an enter with nothing checked is []. The two
    # must not be conflated: None cancels the run, [] means "delete no branches".
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

    # A blank separator sits between the question line and the first choice so the
    # list doesn't crowd the prompt (Separators are non-selectable and unreturned).
    choices: list[questionary.Choice] = [questionary.Separator(" ")]
    for cand, prefix, st in entries:
        title: str | list[tuple[str, str]] = (
            [("", prefix + "   "), (_PT_STYLE.get(st.style, ""), st.label)] if st.label else prefix
        )
        choices.append(questionary.Choice(title=title, value=cand))
    # Breathing room so the picker doesn't render flush against the shell prompt.
    reporter.console.print()
    answer = questionary.checkbox("Select worktrees to tear down", choices=choices).ask()
    chosen: list[resolve.Candidate] = answer or []
    return chosen


def _prompt_numbered(
    reporter: Reporter,
    entries: list[_PickerEntry],
) -> list[resolve.Candidate]:
    console = reporter.console  # stderr — the picker is diagnostic UI, not data
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
            # Clamp BEFORE building the range: a typo like 1-99999999999 must
            # cost at most n iterations, not hang the CLI spinning through it.
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
        help="Worktrees to remove: name, branch, or a unique substring of either. "
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
    keeps the worktree and its record, so the same command retries. A
    worktree locked with `git worktree lock` is never removed.

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

    targets: list[resolve.Candidate] = []
    # Which targets also lose their local branch, by path. The chooser fills this
    # per-worktree; explicit refs take the batch-wide --delete-branch flag.
    branch_delete: set[str] = set()
    # No refs: open the interactive chooser. Selecting there *is* the
    # confirmation, so we skip the extra prompt below (but never the dirty gate).
    # --json / non-TTY must not block on a picker — same contract as the prompt.
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
            return  # nothing to remove, or the user cancelled
        from_chooser = True
    else:
        # Resolve every ref before touching anything: a typo among three targets
        # must not leave the first two half-removed.
        seen: set[str] = set()
        for ref in refs:
            try:
                cand = resolve.resolve_ref(repo_path, cfg.root, ref)
            except resolve.AmbiguousRefError as exc:
                raise _handle(reporter, exc, json_out=json_out) from exc
            except provision.NotFoundError as exc:
                # The worktree may be gone while its branch lingers (manual rm):
                # an exact local-branch match still gets pruned/cleaned up.
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

    # Defense in depth: no resolved target may be the main working tree.
    # Enumeration already excludes it, but the branch-lingering fallback above
    # can still construct the repo's own path when the root contains the repo
    # and a local branch is named like its directory leaf. Removing the main
    # checkout is unrecoverable data loss, so refuse before touching anything.
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

    # The removal gate (uncommitted changes without --force, a git lock) runs
    # here, so a refusal comes before the prompt, and again under the lock
    # right before anything is removed (_gate), so work written while the
    # prompt was open is never deleted. In the chooser a mixed selection is
    # the norm, so blocked ones are skipped and the clean ones the user picked
    # still go (and we say so). For explicit refs it stays all-or-nothing:
    # naming a dirty tree stops the whole run - a scripting contract.
    def _gate(cands: list[resolve.Candidate]) -> list[resolve.Candidate]:
        blocked = [(c, why) for c in cands if (why := _teardown_block(repo_path, c, force))]
        if blocked and not from_chooser:
            cand, why = blocked[0]
            raise _die(
                reporter,
                f"Worktree '{cand.name}' has uncommitted changes."
                if why == "dirty"
                else f"Worktree '{cand.name}' is locked by git (git worktree lock).",
                code=EXIT_CONFLICT,
                error_code="DIRTY_WORKTREE" if why == "dirty" else "WORKTREE_LOCKED",
                hint="Commit/stash the changes, or pass --force to remove anyway."
                if why == "dirty"
                else f"treebox never removes a locked worktree. Unlock it first: "
                f"git -C {repo_path} worktree unlock {cand.path}",
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
        # --json and unattended mode are a scripting contract: never block on
        # a prompt (and never let one leak into stdout) - require --yes.
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
                # The same worktree lock create/enter hold: a teardown must not
                # delete the tree or its container out from under a concurrent
                # provision of the same target. Every lock is taken before any
                # removal starts, so a held lock aborts the whole batch cleanly
                # (LOCK_HELD, exit 5) instead of stopping it halfway through.
                for cand in targets:
                    stack.enter_context(locking.worktree_lock(Path(cand.path)))
                # Recheck under the lock, immediately before removal.
                targets = _gate(targets)
                # --skip-container touches no containers, so it skips runner
                # resolution entirely.
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

    # An incomplete cleanup is a failed teardown: name what remains. Nothing
    # that names it was deleted, so the same command retries.
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

    # Honest exit: the clean ones are gone, but we refused the blocked ones. Do
    # it last so the removals and the recap have already been reported.
    if skipped:
        raise typer.Exit(EXIT_CONFLICT)


# Why the removal gate keeps a worktree: uncommitted changes (without
# --force), or a git lock (never overridden).
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


# What happened to a teardown target's container/runner resources.
ContainerOutcome = Literal["cleaned", "skipped", "failed"]


class TeardownRecord(TypedDict):
    """One worktree's teardown result — emitted verbatim in the ``--json``
    ``worktrees`` array (porcelain discipline applies)."""

    name: str
    branch: str | None
    worktree_path: str
    removed: bool  # False: it was already gone (still exit 0), or it was kept
    branch_deleted: bool
    container: ContainerOutcome
    volumes_removed: bool
    complete: bool  # False: a step failed; the worktree and its record are kept
    remaining: list[str]  # what is still present, "<resource> (<reason>)"


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


# --- doctor ------------------------------------------------------------------


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

    # Instant checks: no I/O worth spinning on, so they print immediately.
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
        checks.append(_agent_login_check(run, h, required=h is selected))

    # Same resolver copy_env uses, so doctor reports the exact secrets source
    # provisioning will copy.
    env_file = (
        provision.resolve_env_file(repo_path, cfg.env_file) if repo_path else Path(cfg.env_file)
    )
    checks.append(DoctorCheck(".env", env_file.is_file(), str(env_file), required=False))

    # Slow checks hit the network / Docker daemon — the source of doctor's "dead
    # pause". Deferred as thunks returning a row plus an optional advisory, so the
    # human path can wrap each in a spinner while --json runs them inline.
    def _check_git_auth() -> DoctorCheck:
        # `create` REQUIRES a fresh fetch, so validate up front that git can
        # authenticate to origin — exercising the same silent paths create uses
        # (ssh-agent, then the HTTPS host-CLI/helper/token fallback).
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
            # Surface the remediation hint as a doctor advisory so the human
            # checklist says how to fix it, not just what is broken.
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
        # Same exit-code contract as the human path: hard checks (git, repo,
        # the selected agent and its login, runner) failing mean exit 1, so
        # `doctor --json && create` in CI can branch on $? instead of `ok`.
        if problems:
            raise typer.Exit(1)
        return

    # The label column is padded to the widest name; every name is known up front
    # (the slow checks' names are fixed), so we can align without buffering rows.
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


# --- version -----------------------------------------------------------------


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


# --- template management -----------------------------------------------------
# Customizing the docker sandbox means owning a copy of the shipped template.
# These commands are the sanctioned way to get and inspect one — no reaching
# into the package with a `python -c 'import treebox...'` one-liner (which
# doesn't even run under an isolated tool install). Everything derives from
# assets.template_dir(), the single resolver the docker runner already uses.

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
    it works from any install and always yields a directory with the full
    required file set. Then edit the Dockerfile and container.json and run
    `treebox create <name> --isolation docker --template <name>`.
    """
    reporter = Reporter(silent=json_out)
    # One identifier rule (models.is_valid_template_name) for init, path, and
    # resolution, so `init` can never write outside the templates root.
    if not is_valid_template_name(name):
        raise _die(
            reporter,
            f"Invalid template name '{name}'. Use letters, digits, '.', '_', or '-'.",
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

    # Copy into a staging dir beside the destination and swap it into place, so
    # a failed copy leaves any existing template untouched rather than deleting
    # it up front — and no OSError escapes as a bare traceback.
    staging = dest.parent / f".{name}.treebox-tmp"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _remove(staging)
        shutil.copytree(src, staging)
    except OSError as exc:
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

    if existing:
        try:
            _remove(dest)
        except OSError as exc:
            with contextlib.suppress(OSError):
                _remove(staging)
            raise _die(
                reporter,
                f"Could not overwrite existing template at {dest}: {exc}.",
                code=EXIT_ERROR,
                error_code="OVERWRITE_FAILED",
                path=str(dest),
                json_out=json_out,
            ) from exc

    try:
        staging.rename(dest)
    except OSError as exc:
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
            continue  # enumerated names resolve by construction; skip if racy
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
    # Advertise the bundled default's contents only when `default` still
    # resolves to the shipped image — a user override or $TREEBOX_TEMPLATE_DIR
    # replaces it with a box whose contents we can't vouch for.
    show_highlights = any(
        r["name"] == assets.DEFAULT_TEMPLATE and r["source"] == "bundled" for r in rows
    )
    reporter.render_template_list(
        rows,
        highlights=assets.DEFAULT_TEMPLATE_HIGHLIGHTS if show_highlights else (),
    )


# Muscle-memory alias, matching the top-level `ls` (hidden; same behavior).
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
    # Restore default SIGPIPE handling on POSIX so piping output into `head` /
    # `grep -m1` ends the process quietly (SIGPIPE, exit 141) instead of
    # surfacing "BrokenPipeError ignored" noise or a traceback at shutdown.
    # Python starts with SIGPIPE ignored, which is wrong for a piped CLI.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    # Pretty, secret-safe tracebacks for genuinely unexpected crashes (handled
    # errors already exit cleanly via _die). Framework frames are suppressed.
    from rich.traceback import install

    install(show_locals=False, suppress=[typer], width=100)


# Muscle-memory aliases (hidden from --help; same options and behavior).
app.command(name="ls", hidden=True)(list_cmd)
app.command(name="rm", hidden=True)(teardown)


if __name__ == "__main__":
    app()
