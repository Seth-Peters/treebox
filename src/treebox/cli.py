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
    forge,
    git,
    locking,
    names,
    provision,
    resolve,
    state,
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
    path_is_under,
    placeholder_branch,
    worktree_path,
    worktree_root,
)
from .output import DoctorCheck, Reporter, StepError, format_age, format_elapsed
from .runners import VALID_ISOLATION, PreflightError, Runner, get_runner

# Stable, documented exit codes (see the epilog on `--help`). Agents branch on these.
EXIT_OK = 0
EXIT_ERROR = 1  # generic / unexpected failure
EXIT_USAGE = 2  # bad invocation (invalid name/branch, ambiguous ref, bad option)
EXIT_NOTFOUND = 3  # the worktree/branch the command needs does not exist
EXIT_PERMISSION = 4  # auth / fetch / credential problem
EXIT_CONFLICT = 5  # already exists / uncommitted changes / lock held

# Current JSON schema version. Payloads only ever *gain* fields within a
# version (git-porcelain discipline); a breaking reshape or rename bumps it.
# Agents branch on these payloads, so treat the shape as a contract.
SCHEMA_VERSION = 1

_EPILOG = (
    "Exit codes: 0 ok · 1 runtime/doctor hard-check · 2 usage · "
    "3 not-found · 4 auth/permission · 5 conflict (exists/locked/dirty)."
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


def _reconcile_with_state(
    reporter: Reporter,
    cfg: Config,
    st: state.WorktreeState | None,
    *,
    isolation: str | None,
    harness: str | None = None,
    template: str | None = None,
    honor_firewall: bool = False,
    json_out: bool = False,
) -> Config:
    """Fold a worktree's recorded creation-time choices into the session config.

    One precedence policy: a recorded value describes the worktree that
    actually exists, so it beats the config default. What an explicit flag
    means differs per field:

    - ``isolation``: the sandbox/no-sandbox decision is fixed at create time —
      falling back to the config default would silently enter a
      docker-sandboxed worktree on the host (or leak its container on
      teardown). An unknown recorded mode (corrupt or hand-edited state) and
      an explicit ``--isolation`` that disagrees with the recorded one are
      both conflicts, never overrides.
    - ``firewall`` (``honor_firewall`` callers, i.e. ``enter``): the recorded
      choice simply wins — capabilities are fixed on the created container,
      so re-resolving the config default would make ``create --no-firewall``
      (under a ``firewall=true`` config) hard-error against its own
      container, and there is no ``enter`` flag to reconcile. ``teardown``
      leaves the config default in place, as it always has.
    - ``harness``: a known recorded value wins over the config default, so
      ``create -H codex`` + ``enter`` launches codex, not the session
      default — but an explicit ``--harness`` stays a legitimate per-session
      override, and an unrecorded/unknown value falls back to the default.
    - ``template``: the template decides the container's user, mounts, and
      per-workspace volume names, so the recorded choice wins (a config
      default here would regenerate ``container.json`` from the wrong
      template — docker exec as the wrong user → unauthenticated agent — and
      derive the wrong volume names on ``--remove-volumes``). An explicit
      ``--template`` stays a legitimate per-session override; unrecorded
      (None) falls back to the config default.

    This is also where recorded/boundary ``str`` names are final: callers
    resolve ``Harness``/runner objects from the returned config, once.
    """
    recorded = st.isolation if st else ""
    if recorded and recorded not in VALID_ISOLATION:
        raise _die(
            reporter,
            f"Worktree was provisioned with unknown isolation mode '{recorded}'.",
            code=EXIT_CONFLICT,
            error_code="UNKNOWN_ISOLATION",
            hint="Its recorded isolation mode is unknown (corrupt or hand-edited state). "
            "Remove it manually (git worktree remove; docker rm any leftover container) "
            "and re-create it.",
            json_out=json_out,
        )
    if recorded and isolation is not None and isolation != recorded:
        raise _die(
            reporter,
            f"Worktree was provisioned with '{recorded}' isolation, "
            f"but --isolation {isolation} was given.",
            code=EXIT_CONFLICT,
            error_code="ISOLATION_MISMATCH",
            hint=f"Drop --isolation to use the recorded '{recorded}' mode, or tear "
            "down and re-create the worktree with the new one.",
            json_out=json_out,
        )
    if recorded and isolation is None:
        cfg = cfg.with_overrides(isolation=recorded)
    if honor_firewall and st is not None:
        cfg = cfg.with_overrides(firewall=st.firewall)
    if harness is None and st and st.harness in VALID_HARNESSES:
        cfg = cfg.with_overrides(harness=st.harness)
    if template is None and st and st.template:
        cfg = cfg.with_overrides(template=st.template)
    return cfg


def _repo_root(reporter: Reporter, repo: str, *, json_out: bool = False) -> str:
    try:
        # Anchor to the *main* worktree so every command sees the same worktree
        # set and root whether invoked from the repo, from .treebox/, or from
        # inside a linked worktree (where --show-toplevel would mislead us).
        return git.main_worktree(expand_user(repo))
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


def _classify(exc: Exception) -> _ErrorInfo:
    """Map a provisioning exception to its exit code / error code / hint."""
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
    if isinstance(exc, provision.BranchInUseError):
        return _ErrorInfo(EXIT_CONFLICT, "BRANCH_IN_USE", exc.hint)
    if isinstance(exc, provision.BranchConflictError):
        return _ErrorInfo(EXIT_CONFLICT, "BRANCH_EXISTS", exc.hint)
    if isinstance(exc, resolve.AmbiguousRefError):
        return _ErrorInfo(EXIT_USAGE, "AMBIGUOUS_REF", exc.hint)
    if isinstance(exc, provision.NotFoundError):
        return _ErrorInfo(EXIT_NOTFOUND, "NOT_FOUND", exc.hint)
    if isinstance(exc, PreflightError):
        # Runner dependency problems keep exit 1 (codes are stable; agents
        # branch on error.code instead: MISSING_DEPENDENCY, DOCKER_UNAVAILABLE).
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


def _emit_result(outcome: provision.Outcome, *, json_out: bool, print_only: bool) -> None:
    """Write the machine/script-facing result to stdout."""
    if json_out:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "name": outcome.worktree.name,
            "worktree_path": str(outcome.worktree.path),
            "branch": outcome.worktree.branch,
            "base": outcome.worktree.base,
            "entry_command": outcome.entry_command,
            "created": outcome.created,
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
        None, "--isolation", help=f"Isolation mode: {'|'.join(VALID_ISOLATION)}."
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Stream raw command output."),
) -> None:
    """Provision a worktree and launch the agent harness in it.

    An explicit NAME becomes the branch, created fresh from origin/<base>
    (feature/auth lives in directory feature--auth). Without NAME or
    --checkout, the worktree starts on a generated treebox/<petname>
    placeholder branch: name the work when it takes shape
    (git branch -m <type>/<short-name>, e.g. feature/user-auth, fix/login-race,
    chore/bump-deps), then push. Every worktree gets a pre-push guard that
    keeps treebox/* refs un-pushable.
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

    if dry_run:
        _dry_run(
            reporter,
            cfg,
            run,
            repo_path,
            wt_name,
            target_branch,
            fetch=not no_fetch,
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
            # Prompt for git credentials whenever a terminal is attached —
            # like `git pull`. ssh's passphrase prompt uses the tty/stderr,
            # so it stays out of the way of --json's stdout.
            interactive=sys.stdin.isatty(),
            reporter=reporter,
            existing_branch=checkout is not None,
        )

    _run_session(
        reporter,
        run,
        agent,
        repo_path=repo_path,
        root=cfg.root,
        name=wt_name,
        provision_call=_provision,
        json_out=json_out,
        print_only=print_only,
        args=[],
    )


# --- enter -------------------------------------------------------------------


@app.command()
def enter(
    ref: str = typer.Argument(..., help="Worktree name, branch, or a unique substring of either."),
    repo: str = typer.Option(".", "--repo", help="Git repo. Default: current repo."),
    root: str | None = typer.Option(None, "--root", help="Worktree root dir."),
    isolation: str | None = typer.Option(
        None, "--isolation", help=f"Isolation mode: {'|'.join(VALID_ISOLATION)}."
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Stream raw command output."),
    args: list[str] | None = typer.Argument(
        None, help="Extra args passed to the agent (after --)."
    ),
) -> None:
    """Re-launch the agent in an existing worktree; refresh .env and re-sync deps if changed."""
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
    st = state.load(cand.path)
    cfg = _reconcile_with_state(
        reporter,
        cfg,
        st,
        isolation=isolation,
        harness=harness,
        template=template,
        honor_firewall=True,
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
            name=cand.name,
            harness=agent,
            cold=cold,
            args=args or [],
            reporter=reporter,
        )

    _run_session(
        reporter,
        run,
        agent,
        repo_path=repo_path,
        root=cfg.root,
        name=cand.name,
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
    repo_path: str,
    root: str,
    name: str,
    provision_call: Callable[[], provision.Outcome],
    json_out: bool,
    print_only: bool,
    args: list[str],
) -> None:
    """The session spine shared by ``create`` and ``enter``: runner preflight,
    the per-name worktree lock, the provisioning step, one exception →
    exit-code classification, then the ``--json`` / ``--print`` / launch fork.

    Preflight runs first so runner-specific preconditions (a no-op for the
    host runner) fail fast with a clean error + hint — before any git state
    is touched, and never surfacing a stopped docker daemon deep inside
    launch as a misleading "no container" that steers users into a
    destructive teardown.
    """
    started = time.monotonic()
    try:
        run.preflight(reporter)
        with locking.worktree_lock(repo_path, root, name):
            outcome = provision_call()
    except _PROVISION_ERRORS as exc:
        raise _handle(reporter, exc, json_out=json_out) from exc

    if json_out or print_only:
        _emit_result(outcome, json_out=json_out, print_only=print_only)
        return
    reporter.blank()
    # Style-A closing line: green "Ready", dim "in <total> — launching <harness>".
    reporter.ready(format_elapsed(time.monotonic() - started), harness.name)
    reporter.blank()
    reporter.restore_terminal()
    try:
        code = run.launch(outcome.worktree, harness=harness, args=args)
    except RuntimeError as exc:
        raise _die(reporter, str(exc)) from exc
    raise typer.Exit(code)


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
        # A registration whose dir is gone (git-prunable, or removed under us)
        # must never be shelled into — that would crash list/teardown on a path
        # that isn't there. Surface it as `missing` so teardown can prune it.
        missing = rec.prunable or not wt_path.is_dir()
        st = state.load(rec.path)
        deps: DepsFreshness
        if missing:
            subject, epoch, deps, env_present = "", 0, "unknown", False
        else:
            env_present = (wt_path / ".env").is_file()
            if st and st.lockfile_hash:
                deps = "fresh" if st.lockfile_hash == ecosystems.lockfile_hash(wt_path) else "stale"
            else:
                deps = "unknown"
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
                base=st.base if st else "",
                isolation=st.isolation if st else "",
                harness=st.harness if st else "",
                deps=deps,
                env="present" if env_present else "absent",
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
    """List worktrees by name with live branch, last commit, age, and dep/.env freshness."""
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

    reporter.render_list(rows, repo_path)


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
    merged_ancestor = git.is_merged_into(repo_path, branch, base) if branch else False
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
        cand = resolve.Candidate(
            name=row["name"],
            branch=None if row["branch"] == "detached" else row["branch"],
            path=row["path"],
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
    isolation: str | None = typer.Option(
        None, "--isolation", help=f"Isolation mode: {'|'.join(VALID_ISOLATION)}."
    ),
    delete_branch: bool = typer.Option(
        False, "--delete-branch", help="Also delete the local branch."
    ),
    remove_volumes: bool = typer.Option(
        False, "--remove-volumes", help="Also remove treebox volumes."
    ),
    force: bool = typer.Option(
        False, "--force", help="Remove even with uncommitted changes / no prompt."
    ),
    skip_container: bool = typer.Option(
        False, "--skip-container", help="Do not touch containers/images."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print a JSON record of what was removed."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Stream raw command output."),
) -> None:
    """Remove worktree directories (and optionally their branches); caches are left intact."""
    reporter = Reporter(quiet=quiet, verbose=verbose, silent=json_out)
    cfg = _resolve_config(
        reporter,
        isolation=isolation,
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
        if json_out or not _stdin_isatty():
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

    # The dirty gate, unified. A worktree with uncommitted changes is unsafe to
    # remove without --force. In the chooser a mixed selection is the norm, so we
    # skip the dirty ones and still remove the clean ones the user picked (and say
    # so). For explicit refs it stays all-or-nothing: naming a dirty tree should
    # stop the whole run — a scripting contract, not a partial surprise.
    def _blocked_dirty(c: resolve.Candidate) -> bool:
        path = Path(c.path)
        # A missing/corrupt .git pointer lets git walk up into the main
        # checkout, so only ask git about dirtiness after linkage is proven.
        return (
            not force
            and path.is_dir()
            and provision.links_to_worktree_gitdir(repo_path, path)
            and git.is_dirty(path)
        )

    skipped_dirty: list[resolve.Candidate] = []
    if from_chooser:
        skipped_dirty = [c for c in targets if _blocked_dirty(c)]
        skipped = {c.path for c in skipped_dirty}
        targets = [c for c in targets if c.path not in skipped]
    else:
        for cand in targets:
            if _blocked_dirty(cand):
                raise _die(
                    reporter,
                    f"Worktree '{cand.name}' has uncommitted changes.",
                    code=EXIT_CONFLICT,
                    error_code="DIRTY_WORKTREE",
                    hint="Commit/stash the changes, or pass --force to remove anyway.",
                    path=cand.path,
                    json_out=json_out,
                )

    if not force and not from_chooser and any(Path(c.path).is_dir() for c in targets):
        # --json is a scripting contract: never block on a prompt (and never
        # let one leak into stdout) — require --force instead.
        if _stdin_isatty() and not json_out:
            listed = ", ".join(c.name for c in targets)
            plural = "s" if len(targets) != 1 else ""
            typer.confirm(f"Remove worktree{plural} {listed}?", abort=True)
        else:
            raise _die(
                reporter,
                "Refusing to remove non-interactively without confirmation.",
                code=EXIT_CONFLICT,
                error_code="NEEDS_CONFIRMATION",
                hint="Pass --force for non-interactive teardown.",
                json_out=json_out,
            )

    records: list[TeardownRecord] = []
    if targets:
        try:
            with contextlib.ExitStack() as stack:
                # The same per-name lock create/enter hold: a teardown must not
                # delete the tree or its container out from under a concurrent
                # provision of the same name. Every lock is taken before any
                # removal starts, so a held lock aborts the whole batch cleanly
                # (LOCK_HELD, exit 5) instead of stopping it halfway through.
                for cand in targets:
                    stack.enter_context(locking.worktree_lock(repo_path, cfg.root, cand.name))
                # Validate the isolation mode for EVERY target before removing
                # anything: a mismatch or unknown recorded mode firing mid-batch
                # would leave the earlier targets removed but unreported in the
                # --json payload. The resolve-everything-first contract above
                # applies to this per-target check too. --skip-container touches
                # no containers, so it skips resolution entirely — the escape
                # hatch for a tree whose recorded mode treebox can't drive.
                runners: list[Runner | None] = (
                    [None] * len(targets)
                    if skip_container
                    else [
                        _teardown_runner(
                            reporter,
                            cfg,
                            cand,
                            repo_path,
                            explicit=isolation,
                            remove_volumes=remove_volumes,
                            json_out=json_out,
                        )
                        for cand in targets
                    ]
                )
                reporter.heading("teardown", ", ".join(c.name for c in targets))
                records = [
                    _teardown_one(
                        reporter,
                        cfg,
                        cand,
                        repo_path,
                        run=run,
                        explicit_isolation=isolation,
                        delete_branch=cand.path in branch_delete,
                        remove_volumes=remove_volumes,
                        force=force,
                        skip_container=skip_container,
                        json_out=json_out,
                    )
                    for cand, run in zip(targets, runners, strict=True)
                ]
        except locking.LockError as exc:
            raise _handle(reporter, exc, json_out=json_out) from exc

    if skipped_dirty:
        _report_skipped_dirty(reporter, skipped_dirty, removed=len(records))

    if json_out:
        _emit_json({"schemaVersion": SCHEMA_VERSION, "worktrees": records})

    # Honest exit: the clean ones are gone, but we refused the dirty ones. Do it
    # last so the removals and the recap have already been reported.
    if skipped_dirty:
        raise typer.Exit(EXIT_CONFLICT)


def _report_skipped_dirty(
    reporter: Reporter, skipped: list[resolve.Candidate], *, removed: int
) -> None:
    """Make the mixed-selection outcome unmistakable: name each worktree we kept
    for having uncommitted changes, how to remove it anyway, and a one-line tally
    so the user knows exactly what did and didn't happen."""
    reporter.blank()
    for cand in skipped:
        reporter.fail(cand.name, "kept · uncommitted changes")
    reporter.hint("commit or stash the changes, or re-run teardown with --force")
    reporter.blank()
    plural = "s" if removed != 1 else ""
    reporter.info(
        f"Removed {removed} worktree{plural} · kept {len(skipped)} with uncommitted changes."
    )


# What happened to a teardown target's container/runner resources.
ContainerOutcome = Literal["cleaned", "skipped", "failed"]


class TeardownRecord(TypedDict):
    """One worktree's teardown result — emitted verbatim in the ``--json``
    ``worktrees`` array (porcelain discipline applies)."""

    name: str
    branch: str | None
    worktree_path: str
    removed: bool  # False: it was already gone (still exit 0)
    branch_deleted: bool
    container: ContainerOutcome
    volumes_removed: bool


def _teardown_runner(
    reporter: Reporter,
    cfg: Config,
    cand: resolve.Candidate,
    repo_path: str,
    *,
    explicit: str | None,
    remove_volumes: bool,
    json_out: bool,
) -> Runner:
    """Resolve one teardown target's runner from its recorded isolation mode.

    The record is read through the repo's own worktree registration
    (`state.load_registered`), never through the worktree's ``.git`` pointer:
    a corrupt tree (missing pointer, the documented teardown recovery path)
    would make the pointer route resolve into the MAIN repo, read no state,
    and silently fall back to the config default - tearing a docker worktree
    down with the host runner and leaking its container while reporting
    ``container: "cleaned"``.

    Called for the whole batch *before* any removal: the mismatch/unknown
    conflicts `_reconcile_with_state` raises must abort an untouched batch
    (all-or-nothing), never fire mid-loop after worktrees are already gone. An
    unknown recorded isolation mode is a hard conflict here just as it is on
    ``enter`` — ``--skip-container`` is the escape hatch for a tree treebox
    can't drive. Reconciliation also recovers the created-time template, so
    `_template_volumes` derives the right per-workspace volume names on
    `--remove-volumes` (teardown has no `--template` flag; state is the only
    source) — and folds the recorded harness too, which nothing in a teardown
    reads today; only the firewall stays at the config default here (see
    ``honor_firewall``). ``remove_volumes`` and the volume names recorded at
    create time are handed to the factory: teardown options are owned by the
    runner, so only runners with per-worktree volumes see them. The recorded
    names are what let `--remove-volumes` still work when the container is
    gone AND the recorded template was deleted (nothing left to derive from);
    a pre-record state (volumes=None) falls back to template derivation."""
    st = state.load_registered(repo_path, cand.path)
    cfg_run = _reconcile_with_state(reporter, cfg, st, isolation=explicit, json_out=json_out)
    return get_runner(
        cfg_run,
        remove_volumes=remove_volumes,
        recorded_volumes=st.volumes if st else None,
    )


def _teardown_one(
    reporter: Reporter,
    cfg: Config,
    cand: resolve.Candidate,
    repo_path: str,
    *,
    run: Runner | None,
    explicit_isolation: str | None,
    delete_branch: bool,
    remove_volumes: bool,
    force: bool,
    skip_container: bool,
    json_out: bool,
) -> TeardownRecord:
    """Tear down one resolved worktree; returns its --json record. ``run`` is
    the batch-validated runner from ``_teardown_runner``, or ``None`` under
    ``--skip-container`` (no container work, so no runner was resolved)."""
    wt = Worktree.locate(repo_path, cfg.root, cand.name, cand.branch or "")
    exists = wt.path.is_dir()
    st = state.load_registered(repo_path, wt.path)

    branch_name = (
        cand.branch
        if cand.branch and (exists or git.local_branch_exists(repo_path, cand.branch))
        else None
    )

    container: ContainerOutcome
    volumes_removed = False
    if skip_container:
        container = "skipped"
        reporter.note("container", "skipped")
    elif not exists and st is None and explicit_isolation is None:
        # The directory and its recorded state are both gone, so the isolation
        # mode is unreadable and the config default is only a guess we won't
        # act on.
        container = "skipped"
        reporter.warn(
            "worktree directory is gone — recorded isolation mode unreadable; "
            "remove any leftover container manually"
        )
    else:
        # run is None only under --skip-container, handled above; a real runner
        # is always resolved when we reach an actual container teardown.
        assert run is not None
        container = "skipped"
        try:
            # The runner was constructed with the batch's volume choice
            # (_teardown_runner); teardown options are its own business.
            teardown_result = run.teardown(wt, reporter=reporter)
            container = teardown_result.container
            volumes_removed = teardown_result.volumes_removed
        except Exception as exc:  # teardown is best-effort
            container = "failed"
            reporter.warn(f"isolation teardown: {exc}")

    if exists:
        try:
            git.worktree_remove(repo_path, wt.path, force=force)
        except git.GitError:
            import shutil as _sh

            _sh.rmtree(wt.path, ignore_errors=True)
            git.worktree_prune(repo_path)
        reporter.ok("worktree", f"removed {_short_path(wt.path, repo_path)}")
    else:
        git.worktree_prune(repo_path)
        reporter.note("worktree", f"already gone · {_short_path(wt.path, repo_path)}")

    branch_deleted = False
    if delete_branch and branch_name:
        try:
            git.delete_branch(repo_path, branch_name)
            branch_deleted = True
            reporter.ok("branch", f"deleted {branch_name}")
        except git.GitError as exc:
            reporter.warn(f"could not delete branch: {exc}")
    elif branch_name:
        reporter.note("branch", f"kept {branch_name}")

    return TeardownRecord(
        name=cand.name,
        branch=branch_name,
        worktree_path=str(wt.path),
        removed=exists,
        branch_deleted=branch_deleted,
        container=container,
        volumes_removed=volumes_removed,
    )


# --- doctor ------------------------------------------------------------------


@app.command()
def doctor(
    repo: str = typer.Option(".", "--repo", help="Git repo. Default: current repo."),
    isolation: str | None = typer.Option(
        None, "--isolation", help=f"Isolation mode to check: {'|'.join(VALID_ISOLATION)}."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Check git, login credentials, UID/GID, and isolation-specific dependencies."""
    reporter = Reporter(silent=json_out)
    cfg = _resolve_config(
        reporter,
        isolation=isolation,
        harness=None,
        base=None,
        root=None,
        firewall=None,
        json_out=json_out,
    )
    run = get_runner(cfg)

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

    for harness in HARNESSES:
        checks.append(
            DoctorCheck(
                f"login: {harness.name}",
                harness.credentials_present(),
                str(harness.credential_path()),
            )
        )

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
    advisories: list[str] = []

    if json_out:
        for _, check in slow:
            result = check()
            checks.append(result)
            if result.advisory:
                advisories.append(result.advisory)
        # Credentials are the only hard gate for the host runner: at least one login.
        has_login = any(c.ok for c in checks if c.name.startswith("login:"))
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "ok": git_ok and bool(repo_path) and (has_login or not run.facts().login_required),
            "isolation": cfg.isolation,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            "advisories": advisories,
        }
        _emit_json(payload)
        # Same exit-code contract as the human path: hard checks (git, repo,
        # runner) failing mean exit 1, so `doctor --json && create` in CI can
        # branch on $? instead of parsing `ok`.
        if _doctor_problems(checks):
            raise typer.Exit(1)
        return

    # The label column is padded to the widest name; every name is known up front
    # (the slow checks' names are fixed), so we can align without buffering rows.
    names_ = [c.name for c in checks] + ["git auth", f"isolation: {run.name}"]
    width = max(len(name) for name in names_)

    checks, advisories = reporter.render_doctor(checks, slow, cfg.isolation, width)
    has_login = any(c.ok for c in checks if c.name.startswith("login:"))
    problems = _doctor_problems(checks)
    reporter.render_doctor_verdict(problems=problems, has_login=has_login, advisories=advisories)
    if problems:
        raise typer.Exit(1)


def _doctor_problems(checks: list[DoctorCheck]) -> list[str]:
    """The hard-check failures (git, repo, isolation) that make doctor exit 1 —
    one definition so the human and --json paths share an exit-code contract."""
    return [
        c.name
        for c in checks
        if not c.ok and (c.name.startswith("isolation") or c.name in ("git", "repo"))
    ]


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

# A template name is a single path segment and a docker-image-tag fragment; keep
# it to a conservative, filesystem-safe charset so `init` can't write outside
# the templates root.
_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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
    if name in (".", "..") or not _TEMPLATE_NAME_RE.match(name):
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
    except RuntimeError as exc:
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
        except RuntimeError:
            continue  # enumerated names resolve by construction; skip if racy
        missing = assets.missing_template_files(path)
        rows.append(
            TemplateRow(
                name=name,
                path=str(path),
                source=_template_source(name),
                default=name == cfg.template,
                valid=not missing,
                missing=missing,
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
    except RuntimeError as exc:
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
) -> None:
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
