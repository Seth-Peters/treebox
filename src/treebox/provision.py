"""Provisioning: the always-host-side half of the tool.

create()/enter() orchestrate the steps the target state lays out:

  fetch -> resolve branch -> worktree add -> push guard (every worktree)
        -> copy submodules -> copy .env -> runner.setup (cache-backed)
        -> record lockfile hash -> hand to runner

Provisioning is identical for every runner; setup/refresh, entry command,
launch, and teardown are the runner-owned seam.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import ecosystems, git, state
from .config import Config
from .harnesses import Harness
from .models import Worktree, expand_user, is_placeholder, same_path, worktree_root
from .output import Reporter
from .runners.base import Runner


class ProvisionError(RuntimeError):
    """Base provisioning failure; ``hint`` (when set) is surfaced under the
    CLI error / in the --json error object."""

    hint: str | None = None


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
) -> tuple[Worktree, list[str]]:
    """The exact git/runner commands ``create`` would run, without side effects."""
    wt = Worktree.locate(repo, config.root, name, branch, base)
    cmds: list[str] = []
    if fetch:
        cmds.append(f"git -C {repo} fetch origin --quiet")
    plan = git.resolve_branch(repo, branch, base)
    if plan.kind == "local":
        cmds.append(f"git -C {repo} worktree add {wt.path} {plan.name}")
    else:
        cmds.append(f"git -C {repo} worktree add -b {plan.name} {wt.path} {plan.start_point}")
    cmds.append(
        "# install pre-push guard: per-worktree core.hooksPath -> "
        f"<private git dir>/{_GUARD_DIR} ({_guard_detail(branch)})"
    )
    if git.has_gitmodules(repo):
        cmds.append(f"# copy submodule trees from {repo} into {wt.path}")
    cmds.append(f"cp {config.env_file} {wt.path}/.env")
    cmds.extend(runner.dry_run_setup(wt))
    return wt, cmds


# --- host-side steps ---------------------------------------------------------


def _copyfile_no_follow(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` without ever following a destination symlink.

    ``dest`` lives inside a freshly checked-out worktree whose tree is untrusted
    (a malicious branch can commit a symlink there, or a boxed agent can plant
    one). ``shutil.copyfile`` would follow such a symlink and write through it to
    an arbitrary host path. Remove any existing entry first, then create the
    destination with ``O_NOFOLLOW | O_EXCL`` so a symlink re-planted in the
    meantime makes the open fail instead of redirecting the write.
    """
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fdst, open(src, "rb") as fsrc:
        shutil.copyfileobj(fsrc, fdst)


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
    dest = worktree / ".env"
    _copyfile_no_follow(src, dest)
    _ensure_env_ignored(worktree)
    reporter.ok("secrets", "copied")
    return True


def _resolve_submodule_copy(
    repo: Path, worktree: Path, rel: str, reporter: Reporter
) -> tuple[Path, Path] | None:
    """Validate an untrusted ``.gitmodules`` path; ``(src, dest)`` or None to skip.

    ``.gitmodules`` is target-repo content, so ``rel`` is attacker-controlled and
    treebox's own parsing bypasses git's submodule-path hardening. Reject
    absolute and ``..``-escaping values, never follow a source that is itself a
    symlink, and require both endpoints to resolve inside their roots — else a
    malicious repo can pull host trees into the agent mount (``vendor -> /home``)
    or copy outside the worktree entirely.
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
    dest = worktree / p
    if not src.resolve().is_relative_to(repo.resolve()) or not dest.resolve().is_relative_to(
        worktree.resolve()
    ):
        reporter.warn(f"skipping submodule escaping the repo: {rel!r}")
        return None
    return src, dest


def copy_submodules(repo: str, worktree: Path, reporter: Reporter) -> int:
    """Copy submodule working trees from the source repo (copy only, no linkage)."""
    if not git.has_gitmodules(repo):
        return 0
    _copyfile_no_follow(Path(repo) / ".gitmodules", worktree / ".gitmodules")
    copied = 0
    for rel in git.submodule_paths(repo):
        resolved = _resolve_submodule_copy(Path(repo), worktree, rel, reporter)
        if resolved is None:
            continue
        src, dest = resolved
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)
        copied += 1
    reporter.ok("submodules", f"copied {copied}")
    return copied


def _record_runner(
    worktree: Worktree, runner: Runner, harness: str, firewall: bool, template: str
) -> None:
    """Persist the runner *before* setup runs. Setup is what builds a docker
    image and starts its container, so if it fails we'd otherwise be left with a
    container and no record of which runner owns it — an un-tearable orphan. With
    the runner on disk first, teardown always knows host vs. docker."""
    state.save(
        worktree.path,
        state.WorktreeState(
            base=worktree.base,
            isolation=runner.name,
            harness=harness,
            lockfile_hash="",
            provisioned=False,
            firewall=firewall,
            template=template,
        ),
    )


def _record_hash(
    worktree: Worktree, runner: Runner, harness: str, firewall: bool, template: str
) -> None:
    state.save(
        worktree.path,
        state.WorktreeState(
            base=worktree.base,
            isolation=runner.name,
            harness=harness,
            lockfile_hash=ecosystems.lockfile_hash(worktree.path),
            provisioned=True,
            firewall=firewall,
            template=template,
        ),
    )


def _finish_setup(
    config: Config,
    runner: Runner,
    wt: Worktree,
    *,
    harness: Harness,
    cold: bool,
    reporter: Reporter,
) -> Outcome:
    """The shared tail of every create path: guard, submodules, .env, setup."""
    ensure_root_ignored(wt.repo, config.root)
    install_push_guard(wt, reporter)
    copy_submodules(wt.repo, wt.path, reporter)
    copy_env(wt.repo, wt.path, config.env_file, reporter)
    # runner on disk before setup can leave a container; firewall + template
    # persisted so `enter`/`teardown` honor the created-time choices instead of
    # the config default.
    _record_runner(wt, runner, harness.name, config.firewall, config.template)
    runner.setup(wt, cold=cold, reporter=reporter)
    _record_hash(wt, runner, harness.name, config.firewall, config.template)
    return Outcome(wt, runner.entry_command(wt, harness=harness, args=[]), created=True)


# --- orchestration -----------------------------------------------------------


def _links_to_worktree_gitdir(repo: str, path: Path) -> bool:
    """Whether ``path`` is a healthy linked worktree of ``repo``: its ``.git``
    pointer FILE resolves to a per-worktree git dir under the repo's common git
    dir. A missing/corrupt pointer (e.g. an interrupted teardown that rm-rf'd it
    but left git's registration) makes git commands inside ``path`` silently
    resolve to the MAIN repo — which we must never "finish setup" against."""
    if not (path / ".git").is_file():
        return False
    try:
        gitdir = Path(git.git_dir(path)).resolve()
        worktrees = (git.common_dir(repo) / "worktrees").resolve()
    except git.GitError:
        return False
    return worktrees in gitdir.parents


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

    # The name is the identity, so an existing directory is a conflict — loud,
    # with the ways out — EXCEPT the crash-recovery case: a prior run created
    # the dir on this exact branch but died before setup finished (state is
    # recorded only after runner.setup completes). Launching into that
    # half-built tree would be the "said Ready, then failed" bug; finish it.
    if wt.path.exists():
        # A healthy treebox worktree links back to its per-worktree git dir via
        # a .git pointer FILE. If that's missing/corrupt the tree is a stray
        # leftover (e.g. an interrupted teardown rm-rf'd the pointer but left
        # git's main-side registration): git commands inside it silently resolve
        # to the MAIN repository, so "finishing setup" would rewrite the real
        # checkout's hooksPath — and state.load would walk up and read None.
        linked = _links_to_worktree_gitdir(repo, wt.path)
        prior = state.load(wt.path) if linked else None
        actual = git.branch_for_path(repo, str(wt.path)) or ""
        # Resume ONLY a genuinely half-built treebox worktree: linkage intact,
        # registered with git on the exact expected branch, setup unfinished.
        # Anything else is a conflict.
        if linked and actual == branch and not (prior is not None and prior.provisioned):
            reporter.note("worktree", "exists but unprovisioned — finishing setup")
            return _finish_setup(config, runner, wt, harness=harness, cold=cold, reporter=reporter)
        conflict = SlugConflictError(name, wt.path)
        if not linked or (not actual and not git.worktree_registered(repo, str(wt.path))):
            conflict.hint = (
                "The directory is not a healthy registered worktree (likely leftover "
                f"from an interrupted teardown). Recover with `treebox teardown {name}` "
                "— it clears both the directory and git's stale registration — then "
                f"re-run. By hand: rm -rf {wt.path} && git -C {repo} worktree prune "
                "(a bare rm -rf leaves the registration behind and re-create fails)."
            )
        raise conflict

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

    # --checkout means "this exact branch": resume work or review a PR. A branch
    # that exists nowhere would silently degrade into "new branch off base" — the
    # placeholder-less path must never invent branches.
    if existing_branch and not (
        git.local_branch_exists(repo, branch) or git.remote_branch_exists(repo, branch)
    ):
        exc = NotFoundError(f"Branch '{branch}' not found locally or on origin.")
        exc.hint = "Fetchable branches only: check the name, or drop --checkout to start new work."
        raise exc

    # An explicit NAME promises a fresh branch off origin/<base> — never a
    # silent adoption of something that already exists. Without this check,
    # resolve_branch below would quietly reuse a local branch or track
    # origin/<name>, turning "start new work" into "resume old work" (a stale-
    # code hazard). Resuming is --checkout's explicitly-asked-for job.
    if not existing_branch and not is_placeholder(branch):
        if git.local_branch_exists(repo, branch):
            raise BranchConflictError(branch, "locally")
        if git.remote_branch_exists(repo, branch):
            raise BranchConflictError(branch, "on origin")

    # An interrupted teardown (working dir removed, registration left behind)
    # leaves a stale entry git flags as prunable: it still holds the branch's
    # checkout lock, so a later `git branch -f` (reset below) or `worktree add`
    # would die with a raw GitError (generic exit 1) — the "followed rm -rf and
    # still stuck" dead-end. Prunable is git's own signal the tree is dead, so
    # clear it and let create self-heal rather than dead-ending on a corpse.
    if any(same_path(rec.path, wt.path) and rec.prunable for rec in git.worktree_list(repo)):
        git.worktree_prune(repo)
        reporter.note("worktree", "pruned stale registration from an interrupted teardown")

    # A branch can back only one worktree at a time: handing a --checkout branch
    # that's already checked out (the main checkout, or another treebox worktree)
    # to `git worktree add` would die with a raw plumbing fatal. Name the
    # collision and the ways out instead. Runs after the prune above so a stale
    # registration git already flagged as dead never counts as "in use".
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

    # The new branch is cut from <base>, and everything below (the stale-
    # placeholder reset, `worktree add`) assumes it resolves. When it exists
    # neither on origin nor locally — a master/trunk-default repo under the
    # default base=main — git would die with a raw `fatal: invalid reference`,
    # so pre-check it and say how to fix it.
    if not existing_branch and not (
        git.remote_branch_exists(repo, base) or git.local_branch_exists(repo, base)
    ):
        exc = NotFoundError(f"Base branch '{base}' not found locally or on origin.")
        exc.hint = (
            "Pass --base <branch> (e.g. --base master for a master-default repo) "
            "or set base in config.toml."
        )
        raise exc

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
    with reporter.task("worktree", detail):
        git.worktree_add(repo, wt.path, plan)

    return _finish_setup(config, runner, wt, harness=harness, cold=cold, reporter=reporter)


def enter(
    config: Config,
    runner: Runner,
    *,
    repo: str,
    name: str,
    harness: Harness,
    cold: bool,
    args: list[str],
    reporter: Reporter,
) -> Outcome:
    """Re-prepare an existing worktree: refresh .env, re-run setup if the
    lockfile changed since last setup or a prior setup never completed
    (recorded provisioned=False). The branch is read live — the agent may
    have renamed it since create."""
    wt = Worktree.locate(repo, config.root, name)
    if not wt.path.is_dir():
        raise NotFoundError(f"Worktree not found: {wt.path}")

    prior = state.load(wt.path)
    branch = git.branch_for_path(repo, str(wt.path)) or ""
    wt = Worktree(
        repo=repo, name=name, branch=branch, base=prior.base if prior else "", path=wt.path
    )

    copy_env(repo, wt.path, config.env_file, reporter)
    # Always-run, like the .env copy: the docker runner re-stages its credential
    # copies here so host logins/logouts propagate on every entry — auth must
    # not ride the lockfile-hash cache that gates setup below.
    runner.refresh(wt, reporter=reporter)

    current = ecosystems.lockfile_hash(wt.path)
    unfinished = prior is None or not prior.provisioned
    changed = prior is None or prior.lockfile_hash != current
    if unfinished or changed:
        if prior is not None and not prior.provisioned:
            reporter.info("setup never completed — finishing setup")
        else:
            reporter.info("dependencies changed since last setup — re-syncing")
        runner.setup(wt, cold=cold, reporter=reporter)
        # Preserve the recorded harness: ``harness`` is this session's launch
        # choice (possibly an explicit one-off -H override), and a dep re-sync
        # must not let it overwrite what the worktree was provisioned with.
        # ``config.firewall`` is safe to re-record as-is: the CLI already
        # reconciled it to the created-time choice (cli._reconcile_with_state).
        # The template is preserved the same way as the harness: a recorded
        # value wins over a config-default ``--template``, so a dep re-sync can
        # never overwrite the created-time template with the config default.
        recorded = prior.harness if prior and prior.harness else harness.name
        recorded_template = prior.template if prior and prior.template else config.template
        _record_hash(wt, runner, recorded, config.firewall, recorded_template)
    else:
        reporter.note("deps", "unchanged · skipping setup")

    return Outcome(wt, runner.entry_command(wt, harness=harness, args=args), created=False)
