"""Provisioning: the always-host-side half of the tool.

create()/enter() orchestrate the steps the target state lays out:

  fetch -> resolve branch -> worktree add -> push guard (every worktree)
        -> copy submodules -> copy .env -> runner.setup (cache-backed)
        -> record the setup result -> hand to runner

Provisioning is identical for every runner; setup/refresh, entry command,
launch, and teardown are the runner-owned seam. Setup results follow the
setup model in ``lifecycle.py``; ``worktree_health`` is the one place a
worktree's health is classified.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import shutil
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import ecosystems, git, lifecycle, locking, ports, record
from .config import Config
from .harnesses import Harness
from .models import (
    Health,
    PortBlock,
    Worktree,
    expand_user,
    is_placeholder,
    same_path,
    worktree_root,
)
from .output import Reporter, StepError
from .runners.base import Runner
from .submodule_copy import SubmoduleCopyError, UnsafeDestinationError, copy_submodule


class ProvisionError(RuntimeError):
    """Base provisioning failure; ``hint`` (when set) is surfaced under the
    CLI error / in the --json error object."""

    hint: str | None = None
    # Set by path-specific failures; surfaced as ``error.path`` under --json.
    path: Path | None = None


class DestinationConflictError(ProvisionError):
    """A host-side copy into the worktree was refused because of what already
    sits at its destination. Nothing is ever removed to make room: the worktree
    tree is untrusted, and the entry may be the user's (or the branch's) own
    content. A submodule copy surfaces this as exit 5, DESTINATION_CONFLICT;
    the managed-file copies (``.env``, ``.gitmodules``) catch it and skip."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.hint = (
            "Inspect this path and move any conflicting entry aside, then retry create. "
            "Treebox does not remove conflicting directories."
        )


class SubmoduleCopyFailedError(ProvisionError):
    """An I/O or permission failure stopped a submodule copy (exit 1,
    SUBMODULE_COPY_FAILED)."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.hint = "Check permissions and free space at this path, then retry create."


class NotFoundError(ProvisionError):
    """The worktree (or, for ``create --checkout``, the branch) a command needs does
    not exist."""


class SlugConflictError(ProvisionError):
    """The requested worktree name is already taken (exit 5, SLUG_CONFLICT)."""

    def __init__(self, name: str, path: Path) -> None:
        super().__init__(f"Worktree '{name}' already exists at {path}.")
        self.hint = (
            f"Enter it (treebox enter {name}), tear it down "
            f"(treebox teardown {name}), or pick another name."
        )


class BranchInUseError(ProvisionError):
    """The requested ``--checkout`` branch already backs another worktree
    (exit 5, BRANCH_IN_USE)."""

    def __init__(self, branch: str, path: str) -> None:
        super().__init__(f"Branch '{branch}' is already checked out at {path}.")
        self.hint = (
            "Each branch can back only one worktree — pick another branch, "
            "or enter/tear down the existing worktree."
        )


class BranchConflictError(ProvisionError):
    """The branch an explicit ``create NAME`` would create already exists
    (exit 5, BRANCH_EXISTS). ``create`` promises a fresh branch off
    origin/<base>; silently adopting an existing one would be a stale-code
    hazard — resuming existing work is ``--checkout``'s job."""

    def __init__(self, branch: str, where: str) -> None:
        super().__init__(f"Branch '{branch}' already exists {where}.")
        self.hint = f"Resume it with `treebox create --checkout {branch}`, or pick another name."


class RecordConflictError(ProvisionError):
    """A resumed ``create`` asked for a different policy than the host-owned
    record of the half-built worktree holds (exit 5, RECORD_CONFLICT)."""

    def __init__(self, name: str, prior: record.WorktreeRecord) -> None:
        super().__init__(
            f"Worktree '{name}' was created with different settings: isolation "
            f"{prior.isolation}, firewall {'on' if prior.firewall else 'off'}, template "
            f"{prior.template}, harness {prior.harness}, base {prior.base}."
        )
        self.path = record.record_path(prior.worktree)
        firewall = "--firewall" if prior.firewall else "--no-firewall"
        self.hint = (
            f"Finish it with the recorded settings: treebox create {name} "
            f"--isolation {prior.isolation} {firewall} --template {prior.template} "
            f"-H {prior.harness} --base {prior.base} - or tear it down "
            f"(treebox teardown {name}) and create it again."
        )


class SetupFailedError(ProvisionError):
    """A setup step exited non-zero (exit 1, SETUP_FAILED). The worktree is
    kept with its setup recorded as failed; ``path`` is the step's output."""

    def __init__(self, wt: Worktree, step: str, exit_status: int, log: Path) -> None:
        super().__init__(
            f"Setup of worktree '{wt.name}' failed: step '{step}' exited with status "
            f"{exit_status}. Its output is in {log}."
        )
        self.path = log
        self.hint = (
            "The worktree is kept and marked unfinished. Fix the cause, then retry the "
            f"setup: treebox enter {wt.name} (or run the same treebox create again)."
        )


class InvalidLinkageError(ProvisionError):
    """The worktree directory exists, but its git linkage is unusable (exit 5,
    INVALID_LINKAGE): git commands inside it would act on the main checkout."""

    def __init__(self, path: Path, health: Health) -> None:
        reason = (
            "its .git pointer is missing or does not lead to its git registration"
            if health == "invalid_linkage"
            else "git has no registration for it"
        )
        super().__init__(f"Worktree '{path.name}' has an invalid git linkage: {reason}.")
        self.path = path
        self.hint = (
            f"Recover with `treebox teardown {path.name}` (it removes the directory and "
            "git's stale registration), then create it again."
            if health == "invalid_linkage"
            else f"Remove the stray directory by hand: rm -rf {path}"
        )


class RecordExistsError(ProvisionError):
    """``adopt`` found a record already in place (exit 5, RECORD_EXISTS)."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"Worktree '{path.name}' already has a host-owned record.")
        self.path = record.record_path(path)
        self.hint = (
            "Nothing to adopt. To replace the record on purpose, add --force "
            "(the settings you pass become the worktree's policy)."
        )


@dataclass
class Outcome:
    worktree: Worktree
    entry_command: list[str]
    created: bool  # False when create() was a no-op on an existing worktree


# Subdir of the worktree's private git dir holding the pre-push guard. Private
# to this worktree (never the shared repo hooks), invisible to `git status`,
# and pruned with the worktree.
_GUARD_DIR = "treebox-hooks"


def install_push_guard(wt: Worktree, reporter: Reporter) -> None:
    """Make ``treebox/*`` refs un-pushable from this worktree: a pre-push hook
    that rejects any such ref, wired via ``extensions.worktreeConfig`` + a
    per-worktree ``core.hooksPath`` — scoped to this worktree only.

    Installed by every ``create`` path, whatever branch the worktree starts
    on: the guard enforces the *prefix* (machine-generated placeholder names
    must be renamed before push), not the starting branch — so a worktree
    created on a real branch still can't push a scratch ``treebox/*`` ref
    someone cuts later.

    The hook lives in the private git dir (``.git/worktrees/<id>/``), which
    sits inside the git common dir the docker runner bind-mounts 1:1 at its
    host path — so the guard binds in-container pushes too, which is the whole
    point: the agent pushes from inside the box.

    This is a forcing function for autonomous agents (they follow error hints;
    the guard makes `git branch -m <real-name>` the only way forward), not a
    security boundary: the agent can edit its own guard, and ``--no-verify``
    bypasses it. Host-side, the per-worktree hooksPath is an agent-writable
    exec redirect of the same class as the shared-config one already documented
    in runners/docker.py — treebox's own git calls neutralize both by pinning
    ``core.hooksPath`` per invocation (git.py ``_SAFE_CONFIG``).
    """
    hooks = Path(git.git_dir(wt.path)) / _GUARD_DIR
    hooks.mkdir(parents=True, exist_ok=True)
    script = resources.files("treebox").joinpath("assets/pre-push").read_text(encoding="utf-8")
    hook = hooks / "pre-push"
    hook.write_text(script, encoding="utf-8")
    hook.chmod(0o755)
    git.enable_worktree_config(wt.path)
    git.set_worktree_hooks_path(wt.path, hooks)
    reporter.ok("push guard", _guard_detail(wt.branch))


def _guard_detail(branch: str) -> str:
    """The guard's one-line consequence for this worktree: a placeholder start
    branch needs a rename before push; any other branch is unaffected."""
    if is_placeholder(branch):
        return f"{branch} is un-pushable until renamed (git branch -m)"
    return "treebox/* refs are un-pushable"


def dry_run_plan(
    config: Config,
    runner: Runner,
    *,
    repo: str,
    name: str,
    branch: str,
    base: str,
    fetch: bool,
    cold: bool,
    existing_branch: bool = False,
) -> tuple[Worktree, list[str]]:
    """A stable git/runner command plan for ``create``, without side effects.

    Checks use locally available refs because this plan must not fetch or write.
    See ``docs/usage.md#dry-run-checks`` for the preflight contract. Runners
    represent disposable paths with stable placeholders.
    """
    wt = Worktree.locate(repo, config.root, name, branch, base)
    resuming = False
    policy = _creation_record(config, runner, wt, config.harness)
    if wt.path.exists():
        resuming = _is_resumable_unprovisioned_worktree(repo, wt, branch)
        if not resuming:
            raise _slug_conflict_for_existing_path(repo, wt)
    else:
        _check_requested_branch(repo, branch=branch, existing_branch=existing_branch)
        _check_checkout_branch_in_use(repo, wt, branch=branch, existing_branch=existing_branch)
        _check_base_exists(repo, base=base, existing_branch=existing_branch)
    prior = _check_resume_policy(wt, policy)
    # Read-only: the block a real run would reserve now (exhaustion fails the
    # dry run like the real one); the plan shows it, the ledger is untouched.
    block = ports.preview(wt.path, config.ports, preferred=prior.ports if prior else None)
    wt = dataclasses.replace(wt, ports=block)
    reserve = (
        f"# reserve port block {block} in {ports.ledger_path()} "
        f"(TREEBOX_PORT_BASE={block.base}, TREEBOX_PORT_COUNT={block.count}; "
        "a dry run reserves nothing)"
    )
    cmds: list[str] = []
    source_ref: str | None = None
    if resuming:
        cmds.append(f"# resume existing unprovisioned worktree at {wt.path}")
        cmds.append(reserve)
    else:
        if fetch:
            cmds.append(f"git -C {repo} fetch origin --quiet")
        plan = git.resolve_branch(repo, branch, base)
        cmds.append(reserve)
        if plan.kind == "local":
            cmds.append(f"git -C {repo} worktree add {wt.path} {plan.name}")
            source_ref = plan.name
        else:
            cmds.append(f"git -C {repo} worktree add -b {plan.name} {wt.path} {plan.start_point}")
            source_ref = plan.start_point
    cmds.append(
        "# install pre-push guard: per-worktree core.hooksPath -> "
        f"<private git dir>/{_GUARD_DIR} ({_guard_detail(branch)})"
    )
    if git.has_gitmodules(repo):
        cmds.append(f"# copy submodule trees from {repo} into {wt.path}")
    cmds.append(f"cp {config.env_file} {wt.path}/.env")
    cmds.extend(runner.dry_run_setup(wt, cold=cold, source_ref=source_ref))
    return wt, cmds


# --- host-side steps ---------------------------------------------------------


def _copyfile_no_follow(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` without ever following a destination symlink.

    ``dest`` lives inside a freshly checked-out worktree whose tree is untrusted
    (a malicious branch can commit a symlink there, or a boxed agent can plant
    one). ``shutil.copyfile`` would follow such a symlink and write through it to
    an arbitrary host path. Remove any existing file or symlink first (an
    unlink never writes through), then create the destination with
    ``O_NOFOLLOW | O_EXCL`` so a symlink re-planted in the meantime makes the
    open fail instead of redirecting the write.

    A *directory* at ``dest`` is never removed - it may be the branch's own
    tracked content - and raises ``DestinationConflictError`` instead, whether
    it was there all along or swapped in mid-copy. Callers decide what a
    refused copy means for them (``_copy_managed_file`` skips it).
    """
    try:
        dest.unlink(missing_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        # unlink() on a directory fails with IsADirectoryError on Linux and
        # PermissionError on macOS; ask the filesystem rather than the errno.
        if _is_directory_no_follow(dest):
            raise DestinationConflictError(
                dest, f"Cannot replace directory with file: {dest}"
            ) from exc
        raise
    with os.fdopen(fd, "wb") as fdst, open(src, "rb") as fsrc:
        shutil.copyfileobj(fsrc, fdst)


def _is_directory_no_follow(path: Path) -> bool:
    """True only when ``path`` itself is a directory (a symlink to one is not)."""
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _copy_managed_file(src: Path, dest: Path, reporter: Reporter) -> bool:
    """Copy a file treebox manages (``.env``, ``.gitmodules``) into the
    worktree; False when the worktree keeps a directory at that name.

    A branch can commit ``.env/`` or ``.gitmodules/`` as a directory, and a
    boxed agent can create one. That must neither crash provisioning nor cost
    the branch its content, so the copy is skipped with a warning and the
    worktree stays usable: the directory is left exactly as it is.
    """
    try:
        _copyfile_no_follow(src, dest)
    except DestinationConflictError:
        reporter.warn(
            f"{dest.name} not copied: {dest} is a directory in this worktree "
            "(left untouched). Rename or remove it on the branch to get the copy."
        )
        return False
    return True


def _ensure_excluded(tree: str | Path, patterns: tuple[str, ...]) -> None:
    """Append ``patterns[0]`` to git's local exclude file for ``tree`` unless
    any of ``patterns`` is already present. Idempotent; never touches tracked
    files or the target repo's own (untrusted) ``.gitignore``."""
    try:
        exclude = git.info_exclude_path(tree)
    except git.GitError:
        return  # not a git worktree; nothing git could ever track
    exclude.parent.mkdir(parents=True, exist_ok=True)
    content = exclude.read_text() if exclude.is_file() else ""
    lines = content.splitlines()
    if any(p in lines for p in patterns):
        return
    if content and not content.endswith("\n"):
        content += "\n"
    exclude.write_text(content + patterns[0] + "\n")


def _ensure_env_ignored(worktree: Path) -> None:
    """Force-ignore the copied ``.env`` via git's local exclude file.

    Whether ``.env`` is ignored must not be delegated to the target repo's
    ``.gitignore`` (untrusted): an autonomous agent's ``git add -A`` would
    otherwise stage and push the secrets treebox just copied in.
    """
    _ensure_excluded(worktree, ("/.env", ".env"))


def ensure_root_ignored(repo: str, root: str) -> None:
    """Force-ignore the worktree root in the target repo's local exclude file.

    Same reasoning as ``.env``: a repo-relative root (the default
    ``.treebox/worktrees``) would otherwise sit as permanent ``?? .treebox/``
    status noise in the main checkout, and a ``git add -A`` there — habitual
    for autonomous agents — would stage every provisioned worktree wholesale.
    A root outside the repo needs no ignoring.
    """
    try:
        rel = worktree_root(repo, root).resolve().relative_to(Path(repo).resolve())
    except ValueError:
        return  # absolute or repo-escaping root: invisible to the repo's git
    if not rel.parts:
        return
    posix = rel.as_posix()
    _ensure_excluded(repo, (f"/{posix}/", f"/{posix}", posix))


def resolve_env_file(repo: str | Path, env_file: str) -> Path:
    """The canonical ``.env`` source: ``env_file`` with a leading ``~``
    expanded, taken as-is when absolute, else resolved against the repo root.
    The single definition of where secrets come from — used by both
    ``copy_env`` and ``doctor`` so they never disagree."""
    src = expand_user(env_file)
    if not src.is_absolute():
        src = Path(repo) / src
    return src


def copy_env(repo: str, worktree: Path, env_file: str, reporter: Reporter) -> bool:
    """Overwrite ``<worktree>/.env`` from the canonical path. Returns True when copied."""
    src = resolve_env_file(repo, env_file)
    if not src.is_file():
        reporter.note("secrets", f"none at {src}")
        return False
    if not _copy_managed_file(src, worktree / ".env", reporter):
        reporter.note("secrets", "not copied · .env is a directory here")
        return False
    _ensure_env_ignored(worktree)
    reporter.ok("secrets", "copied")
    return True


def _safe_submodule_path(repo: Path, rel: str, reporter: Reporter) -> Path | None:
    """Validate an untrusted ``.gitmodules`` path; the repo-relative path to
    copy, or None to skip.

    ``.gitmodules`` is target-repo content, so ``rel`` is attacker-controlled and
    treebox's own parsing bypasses git's submodule-path hardening. Reject
    absolute and ``..``-escaping values, never follow a source that is itself a
    symlink, and require the source to resolve inside its root - else a
    malicious repo can pull host trees into the agent mount (``vendor -> /home``).
    """
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        reporter.warn(f"skipping submodule with unsafe path: {rel!r}")
        return None
    src = repo / p
    if src.is_symlink():
        reporter.warn(f"skipping submodule behind a symlink: {rel!r}")
        return None
    if not src.is_dir():
        return None  # not checked out in the source repo; nothing to copy
    if not src.resolve().is_relative_to(repo.resolve()):
        reporter.warn(f"skipping submodule escaping the repo: {rel!r}")
        return None
    return p


def copy_submodules(repo: str, worktree: Path, reporter: Reporter) -> int:
    """Copy submodule working trees from the source repo (copy only, no linkage)."""
    if not git.has_gitmodules(repo):
        return 0
    _copy_managed_file(Path(repo) / ".gitmodules", worktree / ".gitmodules", reporter)
    copied = 0
    for rel in git.submodule_paths(repo):
        relative = _safe_submodule_path(Path(repo), rel, reporter)
        if relative is None:
            continue
        # The source side is validated above; ``copy_submodule`` owns the
        # destination side (the worktree tree is untrusted) and skips ``.git``
        # pointers, whose relative gitdir would break git in the linked
        # worktree (issue #16).
        try:
            copy_submodule(Path(repo), worktree, relative)
        except UnsafeDestinationError as exc:
            raise DestinationConflictError(exc.path, str(exc)) from exc
        except SubmoduleCopyError as exc:
            raise SubmoduleCopyFailedError(exc.path, str(exc)) from exc
        copied += 1
    reporter.ok("submodules", f"copied {copied}")
    return copied


def _creation_record(
    config: Config, runner: Runner, wt: Worktree, harness: str
) -> record.WorktreeRecord:
    """The policy a ``create`` with this config records for ``wt``."""
    return record.WorktreeRecord(
        worktree=wt.path,
        base=wt.base,
        isolation=runner.name,
        harness=harness,
        firewall=config.firewall,
        template=config.template,
    )


def _check_resume_policy(
    wt: Worktree, policy: record.WorktreeRecord
) -> record.WorktreeRecord | None:
    """The record ``wt`` already has (None when it has none); raise unless it
    states the same policy as ``policy``.

    A resumed create (an earlier run did not finish setup) must restate the
    recorded policy, so a resume is never a way to change the policy of an
    existing worktree. An unreadable record is not overwritten either
    (``adopt --force`` is the explicit way to replace one). Read-only, so
    ``--dry-run`` fails exactly like a real run."""
    try:
        prior = record.load(wt.path)
    except record.RecordMissingError:
        return None
    if not prior.same_policy(policy):
        raise RecordConflictError(wt.name, prior)
    return prior


def _record_policy(
    wt: Worktree, policy: record.WorktreeRecord
) -> tuple[record.WorktreeRecord, bool]:
    """Persist the host-owned record *before* the worktree and any runner
    resource exist, with setup pending and the copy of the port block
    ``wt.ports`` (already in the ledger). A create that dies at any later
    point then leaves a worktree that ``create`` resumes and ``teardown`` can
    drive, never a container with no record of which runner owns it. Returns
    the record and whether this call created it."""
    prior = _check_resume_policy(wt, policy)
    if prior is not None:
        if prior.ports != wt.ports:
            prior = dataclasses.replace(prior, ports=wt.ports)
            record.save(prior)
        return prior, False
    rec = dataclasses.replace(policy, ports=wt.ports)
    record.save(rec)
    return rec, True


def _reserve_ports(
    config: Config, wt: Worktree, recorded: PortBlock | None, reporter: Reporter
) -> tuple[Worktree, bool]:
    """``wt`` with its port block, reserved in the machine-wide ledger
    (``ports.py``) before the record copy is written. ``recorded`` is the
    block the record already holds. Returns whether this call reserved it."""
    block, reserved = ports.reserve(wt.path, wt.repo, config.ports, preferred=recorded)
    if recorded is not None and block != recorded:
        reporter.warn(
            f"port block changed from {recorded} to {block}: the ledger no longer "
            "had this worktree's block and another worktree holds it now"
        )
    reporter.ok("ports", f"{block} · TREEBOX_PORT_BASE={block.base}")
    return dataclasses.replace(wt, ports=block), reserved


def _release_new_block(wt: Worktree, reserved: bool) -> None:
    """Undo a block this create reserved, when the create fails before the
    worktree exists. Best effort: a block left behind is stale (no directory,
    no registration) and the next allocation reclaims it."""
    if reserved:
        with contextlib.suppress(ports.PortsError, locking.LockError, OSError):
            ports.release(wt.path)


def _run_setup(
    runner: Runner, wt: Worktree, rec: record.WorktreeRecord, *, cold: bool, reporter: Reporter
) -> None:
    """Run ``runner.setup`` and record its result in the host-owned record
    (the setup model in ``lifecycle.py``): pending before the attempt, then
    done with the lockfile hash, or failed with the step and its exit status.
    Any other interruption leaves the result pending, which is retried too."""
    rec = dataclasses.replace(rec, setup=lifecycle.SetupResult())
    record.save(rec)
    try:
        runner.setup(wt, cold=cold, reporter=reporter)
    except StepError as exc:
        log = lifecycle.setup_log_path(wt.path)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            exc.log if exc.log.strip() else "(no output captured; --verbose streams it)\n",
            encoding="utf-8",
        )
        record.save(
            dataclasses.replace(rec, setup=lifecycle.SetupResult.failed(exc.label, exc.returncode))
        )
        raise SetupFailedError(wt, exc.label, exc.returncode, log) from exc
    done = lifecycle.SetupResult.done(ecosystems.lockfile_hash(wt.path))
    record.save(dataclasses.replace(rec, setup=done))


def _finish_setup(
    config: Config,
    runner: Runner,
    wt: Worktree,
    rec: record.WorktreeRecord,
    *,
    harness: Harness,
    cold: bool,
    reporter: Reporter,
) -> Outcome:
    """The shared tail of every create path: guard, submodules, .env, setup.
    The record ``rec`` is already on disk, outside every sandbox mount:
    `enter`/`teardown` read the policy back from it, never from config
    defaults or sandbox-writable state."""
    ensure_root_ignored(wt.repo, config.root)
    install_push_guard(wt, reporter)
    copy_submodules(wt.repo, wt.path, reporter)
    copy_env(wt.repo, wt.path, config.env_file, reporter)
    # Runner-owned metadata first, so destructive host cleanup never needs to
    # trust sandbox-writable state.
    runner.initialize(wt)
    _run_setup(runner, wt, rec, cold=cold, reporter=reporter)
    return Outcome(wt, runner.entry_command(wt, harness=harness, args=[]), created=True)


# --- orchestration -----------------------------------------------------------


def worktree_health(repo: str, path: Path) -> Health:
    """The one classification of a worktree's health (``models.Health``),
    shared by create, enter, list (``health`` in --json) and teardown.
    Read-only."""
    if not path.is_dir():
        return "directory_missing"
    admin = git.registered_gitdir(repo, path)
    if admin is None:
        return "unregistered"
    if not _links_to(path, admin):
        return "invalid_linkage"
    try:
        record.load(path)
    except record.RecordError:
        return "unreadable_state"
    return "healthy"


def _links_to(path: Path, admin: Path) -> bool:
    """Whether the ``.git`` pointer FILE of ``path`` leads to exactly its own
    registration dir ``admin``. A missing/corrupt pointer (e.g. an interrupted
    teardown that rm-rf'd it but left git's registration) makes git commands
    inside ``path`` silently resolve to the MAIN repo - which we must never
    "finish setup" against, ask for dirtiness, or remove through git."""
    if not (path / ".git").is_file():
        return False
    try:
        return Path(git.git_dir(path)).resolve() == admin.resolve()
    except git.GitError:
        return False


def _slug_conflict_for_existing_path(repo: str, wt: Worktree) -> SlugConflictError:
    """The conflict for an occupied worktree path, with a recovery hint that
    matches what is actually there (``teardown`` only sees registered
    worktrees, so it is never promised for a directory git does not know)."""
    conflict = SlugConflictError(wt.name, wt.path)
    health = worktree_health(repo, wt.path)
    if health == "healthy":
        return conflict  # the default hint (enter / teardown / pick another name) applies
    if health == "unreadable_state":
        conflict.hint = (
            f"It has no usable host-owned record (a worktree from an older treebox?). "
            f"Restate how it was created: treebox adopt {wt.name} --isolation "
            f"<host|docker> - then enter it - or tear it down: treebox teardown "
            f"{wt.name} --isolation <host|docker>."
        )
    elif health == "invalid_linkage":
        conflict.hint = (
            "The directory is not a healthy registered worktree (likely leftover "
            f"from an interrupted teardown). Recover with `treebox teardown {wt.name}` "
            "— it clears both the directory and git's stale registration — then "
            f"re-run. By hand: rm -rf {wt.path} && git -C {repo} worktree prune "
            "(a bare rm -rf leaves the registration behind and re-create fails)."
        )
    else:
        conflict.hint = (
            "The directory is not a registered worktree (a stray directory git "
            "does not know about), so `treebox teardown` cannot see it. Remove "
            f"it by hand: rm -rf {wt.path} — then re-run."
        )
    return conflict


def _is_resumable_unprovisioned_worktree(repo: str, wt: Worktree, branch: str) -> bool:
    """Whether real create would finish setup in this existing worktree: a
    healthy worktree on the exact expected branch whose host-owned record says
    setup is not done (``lifecycle.resumable``)."""
    if worktree_health(repo, wt.path) != "healthy":
        return False
    actual = git.branch_for_path(repo, str(wt.path)) or ""
    return actual == branch and lifecycle.resumable(record.load(wt.path).setup)


def _check_requested_branch(
    repo: str,
    *,
    branch: str,
    existing_branch: bool,
) -> None:
    """Validate whether create should resume or create the requested branch."""
    if existing_branch and not (
        git.local_branch_exists(repo, branch) or git.remote_branch_exists(repo, branch)
    ):
        exc = NotFoundError(f"Branch '{branch}' not found locally or on origin.")
        exc.hint = "Fetchable branches only: check the name, or drop --checkout to start new work."
        raise exc

    if not existing_branch and not is_placeholder(branch):
        if git.local_branch_exists(repo, branch):
            raise BranchConflictError(branch, "locally")
        if git.remote_branch_exists(repo, branch):
            raise BranchConflictError(branch, "on origin")


def _check_checkout_branch_in_use(
    repo: str,
    wt: Worktree,
    *,
    branch: str,
    existing_branch: bool,
) -> None:
    """Reject a checkout branch that already backs a different worktree."""
    if existing_branch:
        in_use = next(
            (
                rec
                for rec in git.worktree_list(repo)
                if rec.branch == branch and not same_path(rec.path, wt.path)
            ),
            None,
        )
        if in_use is not None:
            raise BranchInUseError(branch, in_use.path)


def _check_base_exists(repo: str, *, base: str, existing_branch: bool) -> None:
    """Reject a new branch whose requested base cannot be resolved."""
    if not existing_branch and not (
        git.remote_branch_exists(repo, base) or git.local_branch_exists(repo, base)
    ):
        exc = NotFoundError(f"Base branch '{base}' not found locally or on origin.")
        exc.hint = (
            "Pass --base <branch> (e.g. --base master for a master-default repo) "
            "or set base in config.toml."
        )
        raise exc


def create(
    config: Config,
    runner: Runner,
    *,
    repo: str,
    name: str,
    branch: str,
    base: str,
    harness: Harness,
    cold: bool,
    fetch: bool,
    interactive: bool,
    reporter: Reporter,
    existing_branch: bool = False,
) -> Outcome:
    """Provision the worktree ``name`` on ``branch`` and prepare it via ``runner``.

    ``branch`` is one of three things: the ``treebox/<name>`` placeholder a
    nameless ``create`` generates, a user-chosen branch created fresh from
    origin/``base`` (an explicit ``create NAME`` — the branch must not exist
    yet), or, with ``existing_branch``, an exact branch that must already
    exist locally or on origin (the ``create --checkout`` path). Every path
    installs the pre-push guard; only placeholders are affected by it.
    """
    wt = Worktree.locate(repo, config.root, name, branch, base)
    policy = _creation_record(config, runner, wt, harness.name)

    # An existing target directory is a conflict except when a prior run created
    # it on this exact branch and setup did not finish (pending or failed in the
    # host-owned record). Launching into that half-built tree would be the
    # "said Ready, then failed" bug; finish it.
    if wt.path.exists():
        # Resume ONLY a genuinely half-built treebox worktree: healthy,
        # registered with git on the exact expected branch, setup unfinished.
        # Anything else is a conflict.
        if _is_resumable_unprovisioned_worktree(repo, wt, branch):
            prior = _check_resume_policy(wt, policy)
            wt, _ = _reserve_ports(config, wt, prior.ports if prior else None, reporter)
            rec, _ = _record_policy(wt, policy)
            reporter.note("worktree", "exists but setup unfinished - finishing setup")
            return _finish_setup(
                config, runner, wt, rec, harness=harness, cold=cold, reporter=reporter
            )
        raise _slug_conflict_for_existing_path(repo, wt)

    # Freshness is the whole point: a fetch is REQUIRED by default and fails
    # loudly (never silently falls back to stale local refs). The only escape is
    # an explicit --no-fetch, or a repo with no origin (nothing to be fresh
    # against). When a TTY is attached, git/ssh may prompt for credentials.
    if fetch:
        if not git.has_origin(repo):
            reporter.note("fetch", "no origin remote; using local refs")
        elif interactive:
            # A terminal is attached: try the silent credential paths first and,
            # only if they fail, let git/ssh prompt for a passphrase. No spinner
            # here so the prompt owns the terminal cleanly (like `git pull`).
            reporter.info("fetching origin (you may be prompted for credentials)…")
            git.fetch_origin(repo, required=True, interactive=True)
            reporter.ok("fetch", "origin up to date")
        else:
            with reporter.task("fetch", "origin up to date"):
                git.fetch_origin(repo, required=True, interactive=False)
    else:
        reporter.note("fetch", "skipped (--no-fetch) · refs may be stale")

    _check_requested_branch(repo, branch=branch, existing_branch=existing_branch)

    # An interrupted teardown (working dir removed, registration left behind)
    # leaves a stale entry git flags as prunable: it still holds the branch's
    # checkout lock, so a later `git branch -f` (reset below) or `worktree add`
    # would die with a raw GitError (generic exit 1) — the "followed rm -rf and
    # still stuck" dead-end. Prunable is git's own signal the tree is dead, so
    # clear it and let create self-heal rather than dead-ending on a corpse.
    if any(same_path(rec.path, wt.path) and rec.prunable for rec in git.worktree_list(repo)):
        git.worktree_prune(repo)
        reporter.note("worktree", "pruned stale registration from an interrupted teardown")

    # A branch can back only one worktree. Keep this after prune so a stale
    # registration at the requested path is self-healed before the check.
    _check_checkout_branch_in_use(repo, wt, branch=branch, existing_branch=existing_branch)

    # Everything below assumes a resolvable base for new work.
    _check_base_exists(repo, base=base, existing_branch=existing_branch)

    # A lingering local placeholder (teardown keeps branches by default) must
    # not silently bypass the freshness invariant: it is a guaranteed-unpushed
    # scratch ref, so when its tip is already contained in the fresh
    # origin/<base> it is safe to fast-forward it there. A diverged placeholder
    # carries unpushed work we must not destroy — resume it, but say so loudly
    # instead of claiming freshness. Placeholders ONLY: a user-named branch is
    # never reset (it can't reach here anyway — BranchConflictError above).
    if not existing_branch and is_placeholder(branch) and git.local_branch_exists(repo, branch):
        start = f"origin/{base}" if git.remote_branch_exists(repo, base) else base
        if git.is_merged_into(repo, branch, base):
            git.update_branch(repo, branch, start)
            reporter.note("branch", f"stale placeholder {branch} reset to {start}")
        else:
            reporter.warn(
                f"placeholder {branch} has commits not on {start} — resuming that "
                f"work, NOT a fresh {start}. To start over: "
                f"treebox teardown {name} --delete-branch"
            )

    plan = git.resolve_branch(repo, branch, base)
    # Runner-specific preconditions (e.g. the docker runner's daemon check)
    # live in runner.preflight, which the CLI runs before provisioning — no
    # duplicate gate here.
    wt.path.parent.mkdir(parents=True, exist_ok=True)
    detail = {
        "local": f"on {plan.name}",
        "track-remote": f"tracking {plan.start_point}",
        "new": f"{plan.name} from {plan.start_point}",
    }[plan.kind]
    # The port block first (a full range fails here, before anything exists),
    # then the record copy, then the worktree.
    prior = _check_resume_policy(wt, policy)
    wt, reserved = _reserve_ports(config, wt, prior.ports if prior else None, reporter)
    try:
        rec, wrote = _record_policy(wt, policy)
    except BaseException:
        _release_new_block(wt, reserved)
        raise
    try:
        with reporter.task("worktree", detail):
            git.worktree_add(repo, wt.path, plan)
    except BaseException:
        if wrote:
            record.remove(wt.path)  # nothing exists yet that the record could describe
        _release_new_block(wt, reserved)
        raise

    return _finish_setup(config, runner, wt, rec, harness=harness, cold=cold, reporter=reporter)


def enter(
    config: Config,
    runner: Runner,
    *,
    repo: str,
    path: Path,
    base: str,
    harness: Harness,
    cold: bool,
    args: list[str],
    reporter: Reporter,
) -> Outcome:
    """Re-prepare an existing worktree: refresh .env, and run setup when the
    setup model says so (``lifecycle.setup_reason``: setup pending or failed,
    or the lockfile changed since it finished). The branch is read live - the
    agent may have renamed it since create. ``config`` is already reconciled
    with the host-owned record (``cli._reconcile_with_record``) and ``base``
    is the recorded base; nothing here reads sandbox-writable state."""
    # ``path`` is the exact registered path the ref resolved to (possibly a
    # descendant of the configured root), never a root/name reconstruction.
    wt = Worktree.at(repo, path)
    _require_usable(repo, wt.path)
    # Re-read under the lock: the setup result may have changed since the
    # caller read the policy.
    rec = record.load(wt.path)
    branch = git.branch_for_path(repo, str(wt.path)) or ""
    wt = Worktree.at(repo, path, branch=branch, base=base)
    # The ledger entry first, then the record copy (a record from an older
    # treebox has none: this reserves its first block).
    wt, _ = _reserve_ports(config, wt, rec.ports, reporter)
    if rec.ports != wt.ports:
        rec = dataclasses.replace(rec, ports=wt.ports)
        record.save(rec)

    copy_env(repo, wt.path, config.env_file, reporter)
    # Always-run, like the .env copy: the docker runner re-stages its credential
    # copies here so host logins/logouts propagate on every entry — auth must
    # not ride the lockfile-hash cache that gates setup below.
    runner.refresh(wt, reporter=reporter)

    reason = lifecycle.setup_reason(rec.setup, ecosystems.lockfile_hash(wt.path))
    if reason is not None:
        reporter.info(reason)
        _run_setup(runner, wt, rec, cold=cold, reporter=reporter)
    else:
        reporter.note("deps", "unchanged · skipping setup")

    return Outcome(wt, runner.entry_command(wt, harness=harness, args=args), created=False)


def adopt(repo: str, path: Path, policy: record.WorktreeRecord, *, force: bool) -> None:
    """Write the host-owned record for an existing worktree that has none (a
    legacy worktree from before records existed) or, with ``force``, replace
    an existing one. This is the operator's explicit migration step: the
    policy comes only from what the operator states, never from the
    worktree's sandbox-writable state. The setup result starts pending, so
    the next ``enter`` runs setup under the adopted policy. Callers hold the
    worktree lock."""
    _require_usable(repo, path)
    if record.exists(path) and not force:
        raise RecordExistsError(path)
    record.save(dataclasses.replace(policy, setup=lifecycle.SetupResult()))


def _require_usable(repo: str, path: Path) -> None:
    """Refuse a worktree whose directory or git linkage is unusable, by its
    health: without a healthy ``.git`` pointer every git command (and the
    launched agent) would act on the MAIN checkout."""
    health = worktree_health(repo, path)
    if health == "directory_missing":
        raise NotFoundError(f"Worktree not found: {path}")
    if health in ("invalid_linkage", "unregistered"):
        raise InvalidLinkageError(path, health)
