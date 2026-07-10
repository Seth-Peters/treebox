"""End-to-end host-runner flow against a real local git repo.

Exercises the whole target-state create/enter/list/teardown path: the
name-as-identity arg surface (explicit name-as-branch / petname / --checkout),
the treebox/<petname> placeholder branch and the pre-push guard installed in
every worktree, branch resolution from origin/<base>, submodule copy, .env
copy, cache-backed setup, lockfile-hash-driven re-sync, and ref resolution
for enter/teardown.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox import state
from treebox.cli import SCHEMA_VERSION, app

runner = CliRunner()


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path / "wts")


def _run(args: list[str]):
    return runner.invoke(app, args, catch_exceptions=False)


def _git(wt: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True)


def test_create_provisions_everything(repo: Path, root: str, hermetic_config):
    from treebox import git

    wt = Path(root) / "feature-auth"
    res = _run(
        [
            "create",
            "feature-auth",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--print",
        ]
    )
    assert res.exit_code == 0, res.output
    # Name-named directory; an explicit name IS the branch (no placeholder).
    assert wt.is_dir()
    assert git.branch_for_path(str(repo), str(wt)) == "feature-auth"
    # Fresh secrets copied from canonical .env.
    assert (wt / ".env").read_text() == "SECRET=canonical\n"
    # Submodule working tree copied (copy only).
    assert (wt / "sub" / "lib.txt").read_text() == "hello from submodule\n"
    # Setup hook ran exactly once.
    assert (wt / "setup.log").read_text().strip() == "ran"
    # Lockfile hash recorded.
    st = state.load(wt)
    assert st and st.lockfile_hash and st.base == "dev" and st.isolation == "host"
    # Launch command printed to stdout.
    assert "claude" in res.stdout
    # The pre-push guard is wired per-worktree, into the private git dir.
    hooks_path = _git(wt, "config", "--worktree", "core.hooksPath")
    assert hooks_path.returncode == 0
    hook = Path(hooks_path.stdout.strip()) / "pre-push"
    assert hook.is_file()
    assert hook.stat().st_mode & 0o111  # executable
    assert str(git.git_dir(wt)) in str(hook)  # lives in .git/worktrees/<id>/


def test_create_generates_a_petname_when_name_is_omitted(repo: Path, root: str, hermetic_config):
    import re

    from treebox import git

    res = _run(["create", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    name = payload["name"]
    assert re.fullmatch(r"[a-z]+-[a-z]+(-\d+)?", name)
    assert payload["branch"] == f"treebox/{name}"
    assert (Path(root) / name).is_dir()
    assert git.branch_for_path(str(repo), str(Path(root) / name)) == f"treebox/{name}"


def test_create_invalid_name_is_usage_error(repo: Path, root: str, hermetic_config):
    # "treebox/x" is shaped like a valid name but reserved: it would masquerade
    # as a placeholder and be un-pushable by construction.
    for bad in ("bad name", "Bad-Case", "trailing/", "fix//x", "treebox/x"):
        res = _run(["create", bad, "--repo", str(repo), "--root", root, "--json"])
        assert res.exit_code == 2, bad  # EXIT_USAGE
        assert json.loads(res.stderr)["error"]["code"] == "INVALID_NAME"


def test_create_ref_invalid_name_is_usage_error(repo: Path, root: str, hermetic_config):
    # A name is slug-valid but not a valid git ref: since the name IS the branch,
    # a leading-dash (or all-dash) token would be swallowed as a `git branch` flag.
    # It must fail cleanly with INVALID_BRANCH / exit 2, not a raw git usage dump.
    for bad in ("-foo", "-", "---"):
        res = _run(
            ["create", "--repo", str(repo), "--root", root, "--json", "--no-fetch", "--", bad]
        )
        assert res.exit_code == 2, bad  # EXIT_USAGE
        assert json.loads(res.stderr)["error"]["code"] == "INVALID_BRANCH", bad


def test_create_explicit_name_is_the_branch_and_pushable(repo: Path, root: str, hermetic_config):
    """An explicit name is intentional: it becomes the branch, created fresh
    from origin/<base>, pushable immediately. The guard is still installed but
    only bites treebox/* refs."""
    from treebox import git

    res = _run(["create", "fix-login", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["name"] == "fix-login"
    assert payload["branch"] == "fix-login"
    wt = Path(root) / "fix-login"
    assert git.branch_for_path(str(repo), str(wt)) == "fix-login"
    # No rename needed: the branch pushes as-is…
    assert _git(wt, "push", "origin", "fix-login").returncode == 0
    # …but the guard is wired and still blocks any treebox/* ref from here.
    assert _git(wt, "config", "--worktree", "core.hooksPath").returncode == 0
    assert _git(wt, "branch", "treebox/scratch").returncode == 0
    push = _git(wt, "push", "origin", "treebox/scratch")
    assert push.returncode != 0
    assert "refusing to push placeholder branch 'treebox/scratch'" in push.stderr


def test_create_slash_name_flattens_dir_and_keeps_branch(repo: Path, root: str, hermetic_config):
    from treebox import git

    res = _run(["create", "feature/auth", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["name"] == "feature--auth"
    assert payload["branch"] == "feature/auth"
    wt = Path(root) / "feature--auth"
    assert wt.is_dir()
    assert git.branch_for_path(str(repo), str(wt)) == "feature/auth"


def test_create_existing_branch_is_branch_exists_conflict(repo: Path, root: str, hermetic_config):
    """create NAME promises a fresh branch off origin/<base>: a branch that
    already exists (locally or on origin) is a loud exit 5 pointing at
    --checkout — never a silent adoption of old work."""
    subprocess.run(["git", "-C", str(repo), "branch", "taken"], check=True)
    res = _run(["create", "taken", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5, res.output  # EXIT_CONFLICT
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "BRANCH_EXISTS"
    assert "--checkout taken" in err["error"]["hint"]
    assert not (Path(root) / "taken").exists()

    # Remote-only branches conflict too (a local one would diverge from it).
    subprocess.run(
        ["git", "-C", str(repo), "push", "-q", "origin", "taken:remote-taken"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    res = _run(["create", "remote-taken", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5, res.output
    assert json.loads(res.stderr)["error"]["code"] == "BRANCH_EXISTS"
    assert not (Path(root) / "remote-taken").exists()


def test_create_same_name_after_teardown_conflicts_with_kept_branch(
    repo: Path, root: str, hermetic_config
):
    """teardown keeps the branch by default, so re-creating the same explicit
    name hits BRANCH_EXISTS — the branch may hold real work; --checkout (or
    teardown --delete-branch) is the deliberate way forward."""
    base = ["--repo", str(repo), "--root", root]
    assert _run(["create", "redo", *base, "--print"]).exit_code == 0
    assert _run(["teardown", "redo", *base, "--force"]).exit_code == 0

    res = _run(["create", "redo", *base, "--json"])
    assert res.exit_code == 5, res.output
    assert json.loads(res.stderr)["error"]["code"] == "BRANCH_EXISTS"

    # The hinted recovery works and lands on the kept branch.
    res = _run(["create", "--checkout", "redo", *base, "--print"])
    assert res.exit_code == 0, res.output


def test_create_name_collision_is_conflict(repo: Path, root: str, hermetic_config):
    """The name is the identity: creating a taken name is a loud exit 5 with
    the ways out (enter / teardown / another name) — never a silent reuse."""
    assert _run(["create", "x", "--repo", str(repo), "--root", root, "--print"]).exit_code == 0
    res = _run(["create", "x", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5  # EXIT_CONFLICT
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "SLUG_CONFLICT"
    assert "enter" in err["error"]["hint"] and "teardown" in err["error"]["hint"]


def test_create_finishes_unprovisioned_worktree(repo: Path, root: str, hermetic_config):
    """A worktree dir left behind by a run that died before setup (no recorded
    state) must be finished on the next create — not launched half-built, and
    not refused as a name conflict."""
    wt = Path(root) / "feature-auth"
    args = ["create", "feature-auth", "--repo", str(repo), "--root", root, "--print"]
    assert _run([*args, "--base", "dev"]).exit_code == 0
    assert (wt / "setup.log").read_text().strip() == "ran"

    # Simulate the half-provisioned state: dir exists, but setup never completed.
    st = state._state_path(wt)
    st.unlink()
    (wt / "setup.log").unlink()

    res = _run([*args, "--base", "dev"])
    assert res.exit_code == 0, res.output
    # Setup re-ran (instead of the old behaviour: skip setup, launch, fail).
    assert (wt / "setup.log").read_text().strip() == "ran"
    assert state.load(wt) is not None  # state recorded this time


def test_placeholder_branch_is_unpushable_until_renamed(repo: Path, root: str, hermetic_config):
    """The core forcing function: a treebox/* placeholder can never reach a PR.
    Real `git push` against the local origin must be rejected by the pre-push
    guard with the instructive message; `git branch -m` clears it."""
    res = _run(["create", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    branch = payload["branch"]
    assert branch == f"treebox/{payload['name']}"
    wt = Path(root) / payload["name"]

    push = _git(wt, "push", "origin", branch)
    assert push.returncode != 0
    assert f"refusing to push placeholder branch '{branch}'" in push.stderr
    assert "git branch -m" in push.stderr

    # Naming the work clears the guard: same worktree, same hook, push passes.
    assert _git(wt, "branch", "-m", "fix-guarded-work").returncode == 0
    push = _git(wt, "push", "origin", "fix-guarded-work")
    assert push.returncode == 0, push.stderr


def test_create_b_uses_the_exact_branch_with_inert_guard(repo: Path, root: str, hermetic_config):
    from treebox import git

    subprocess.run(["git", "-C", str(repo), "branch", "preexisting"], check=True)
    res = _run(
        ["create", "--checkout", "preexisting", "--repo", str(repo), "--root", root, "--print"]
    )
    assert res.exit_code == 0, res.output
    wt = Path(root) / "preexisting"
    assert git.branch_for_path(str(repo), str(wt)) == "preexisting"
    # The guard is installed in every worktree, but a real branch pushes freely.
    assert _git(wt, "config", "--worktree", "core.hooksPath").returncode == 0
    assert _git(wt, "push", "origin", "preexisting").returncode == 0


def test_create_b_missing_branch_is_not_found(repo: Path, root: str, hermetic_config):
    res = _run(
        ["create", "--checkout", "ghost/branch", "--repo", str(repo), "--root", root, "--json"]
    )
    assert res.exit_code == 3  # EXIT_NOTFOUND
    assert json.loads(res.stderr)["error"]["code"] == "NOT_FOUND"
    assert not (Path(root) / "ghost--branch").exists()


def test_create_b_remote_only_branch_is_tracked(repo: Path, root: str, hermetic_config):
    """create --checkout for a branch that exists on origin but NOT locally must take
    the 'track-remote' plan: branch from origin/<name> (a teammate's pushed
    work), not from the base — and track the remote branch."""
    from treebox import git

    def _g(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.DEVNULL)

    def _rev(cwd: Path, ref: str) -> str:
        return subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", ref],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    # Publish a teammate's branch: a distinct commit on origin, then drop the
    # local ref so only refs/remotes/origin/teammate/topic remains.
    _g("checkout", "-b", "teammate/topic")
    (repo / "remote-only.txt").write_text("from teammate\n")
    _g("add", "remote-only.txt")
    _g("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-m", "remote work")
    _g("push", "origin", "teammate/topic")
    _g("checkout", "main")
    _g("branch", "-D", "teammate/topic")

    res = _run(
        ["create", "--checkout", "teammate/topic", "--repo", str(repo), "--root", root, "--print"]
    )
    assert res.exit_code == 0, res.output
    wt = Path(root) / "teammate--topic"  # derived name: slashes flatten to --
    # The worktree is ON the branch, materialized at the remote commit.
    assert git.branch_for_path(str(repo), str(wt)) == "teammate/topic"
    assert (wt / "remote-only.txt").read_text() == "from teammate\n"
    assert _rev(wt, "HEAD") == _rev(repo, "origin/teammate/topic")
    assert _rev(wt, "HEAD") != _rev(repo, "origin/main")  # not branched from base
    # The new local branch tracks origin/<name>.
    upstream = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "@{upstream}"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert upstream == "origin/teammate/topic"


def test_create_b_collision_on_derived_name_is_conflict(repo: Path, root: str, hermetic_config):
    subprocess.run(["git", "-C", str(repo), "branch", "preexisting"], check=True)
    args = ["create", "--checkout", "preexisting", "--repo", str(repo), "--root", root]
    assert _run([*args, "--print"]).exit_code == 0
    res = _run([*args, "--json"])
    assert res.exit_code == 5  # EXIT_CONFLICT
    assert json.loads(res.stderr)["error"]["code"] == "SLUG_CONFLICT"


def test_create_b_branch_in_main_checkout_is_conflict(repo: Path, root: str, hermetic_config):
    # `create --checkout main` — sandboxing the currently-checked-out default
    # branch — must be a clean BRANCH_IN_USE conflict naming the collision, not
    # git's raw `fatal: '...' is already used by worktree` with a generic exit 1.
    res = _run(["create", "--checkout", "main", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5  # EXIT_CONFLICT
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "BRANCH_IN_USE"
    assert "already checked out" in err["message"] and str(repo) in err["message"]
    assert "fatal:" not in err["message"]  # no raw git plumbing output
    assert "hint" in err
    assert not (Path(root) / "main").exists()


def test_create_b_branch_in_other_worktree_is_conflict(repo: Path, root: str, hermetic_config):
    # Same collision against another treebox worktree instead of the main checkout.
    subprocess.run(["git", "-C", str(repo), "branch", "shared"], check=True)
    args = ["--repo", str(repo), "--root", root]
    assert _run(["create", "--checkout", "shared", *args, "--print"]).exit_code == 0
    res = _run(["create", "dup", "--checkout", "shared", *args, "--json"])
    assert res.exit_code == 5  # EXIT_CONFLICT
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "BRANCH_IN_USE"
    assert str(Path(root) / "shared") in err["message"]
    assert not (Path(root) / "dup").exists()


def test_create_missing_base_is_not_found(repo: Path, root: str, hermetic_config):
    # A base that exists neither on origin nor locally (e.g. the default
    # base=main against a master-default repo) must be a clean NOT_FOUND with a
    # --base hint, not git's raw `fatal: invalid reference` with exit 1.
    res = _run(
        ["create", "myfeature", "--base", "ghost", "--repo", str(repo), "--root", root, "--json"]
    )
    assert res.exit_code == 3  # EXIT_NOTFOUND
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "NOT_FOUND"
    assert "Base branch 'ghost'" in err["message"]
    assert "--base" in err["hint"]
    assert not (Path(root) / "myfeature").exists()


def test_create_default_base_missing_in_master_repo_is_not_found(tmp_path: Path, hermetic_config):
    # The first-run wall from issue #140: a plain `treebox create` in a repo
    # whose default branch is master (no main anywhere) names the missing base
    # instead of dying on `git worktree add`.
    origin = tmp_path / "m-origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)], check=True)
    work = tmp_path / "m-work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    (work / "README").write_text("hi\n")
    env_git = ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-C", str(work)]
    subprocess.run([*env_git, "add", "-A"], check=True)
    subprocess.run([*env_git, "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run([*env_git, "push", "origin", "master"], check=True, capture_output=True)

    root = str(tmp_path / "m-wts")
    res = _run(["create", "myfeature", "--repo", str(work), "--root", root, "--json"])
    assert res.exit_code == 3  # EXIT_NOTFOUND
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "NOT_FOUND"
    assert "Base branch 'main'" in err["message"]
    assert "--base" in err["hint"]
    # And the hinted fix works.
    res = _run(
        ["create", "myfeature", "--base", "master", "--repo", str(work), "--root", root, "--print"]
    )
    assert res.exit_code == 0, res.output


def test_enter_resolves_name_branch_and_substring(repo: Path, root: str, hermetic_config):
    """enter accepts the name, the live branch (even after the agent renamed
    it), or a unique substring of either; ambiguity is a loud exit 2."""
    _run(["create", "fix-auth", "--repo", str(repo), "--root", root, "--print"])
    _run(["create", "fix-authz", "--repo", str(repo), "--root", root, "--print"])
    base = ["--repo", str(repo), "--root", root, "--print"]

    # Exact name wins even when it is also a substring of another worktree.
    assert _run(["enter", "fix-auth", *base]).exit_code == 0
    # Live branch: rename in the worktree, then enter by the new branch name.
    assert _git(Path(root) / "fix-auth", "branch", "-m", "speed-up-ci").returncode == 0
    assert _run(["enter", "speed-up-ci", *base]).exit_code == 0
    # Unique substring of a branch.
    assert _run(["enter", "up-ci", *base]).exit_code == 0
    # Ambiguous substring: exit 2 with the structured code.
    res = _run(["enter", "authz", *base])
    assert res.exit_code == 0  # unique: only fix-authz matches
    res = _run(["enter", "fix-", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 2  # EXIT_USAGE
    assert json.loads(res.stderr)["error"]["code"] == "AMBIGUOUS_REF"


def test_enter_skips_setup_when_unchanged(repo: Path, root: str, hermetic_config):
    wt = Path(root) / "feature-auth"
    _run(
        [
            "create",
            "feature-auth",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--print",
        ]
    )
    # Wipe .env to prove enter refreshes it.
    (wt / ".env").unlink()
    res = _run(["enter", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    assert res.exit_code == 0, res.output
    assert (wt / ".env").read_text() == "SECRET=canonical\n"  # refreshed
    assert (wt / "setup.log").read_text().strip() == "ran"  # NOT re-run


def test_enter_resyncs_when_lockfile_changes(repo: Path, root: str, hermetic_config):
    wt = Path(root) / "feature-auth"
    _run(
        [
            "create",
            "feature-auth",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--print",
        ]
    )
    (wt / "uv.lock").write_text("version = 2\n")  # deps landed on the branch
    res = _run(["enter", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    assert res.exit_code == 0, res.output
    assert (wt / "setup.log").read_text().splitlines() == ["ran", "ran"]  # re-ran


def test_enter_uses_recorded_harness(repo: Path, root: str, hermetic_config):
    """enter without -H must launch the harness the worktree was provisioned
    with, not the config default — both harnesses launch fully autonomous, so
    silently starting the wrong one matters."""
    base = ["--repo", str(repo), "--root", root]
    res = _run(["create", "qux", *base, "-H", "codex", "--print"])
    assert res.exit_code == 0, res.output
    assert "codex" in res.stdout and "claude" not in res.stdout

    res = _run(["enter", "qux", *base, "--print"])
    assert res.exit_code == 0, res.output
    # Recorded harness wins. Assert on the harness named in the printed
    # command, not its exact shape — that is the host runner's business.
    assert "codex" in res.stdout and "claude" not in res.stdout

    # Unlike isolation, an explicit -H is a legitimate per-session override.
    res = _run(["enter", "qux", *base, "-H", "claude", "--print"])
    assert res.exit_code == 0, res.output
    assert "claude" in res.stdout and "codex" not in res.stdout


def test_enter_resync_preserves_recorded_harness(repo: Path, root: str, hermetic_config):
    """A dep re-sync on enter re-records state; that must not stamp the session
    default — or a one-off -H override — over the recorded harness."""
    base = ["--repo", str(repo), "--root", root]
    _run(["create", "qux", *base, "-H", "codex", "--print"])
    wt = Path(root) / "qux"

    (wt / "uv.lock").write_text("version = 2\n")  # deps changed → setup re-runs
    res = _run(["enter", "qux", *base, "-H", "claude", "--print"])
    assert res.exit_code == 0, res.output
    assert "claude" in res.stdout and "codex" not in res.stdout  # override ran…
    st = state.load(wt)
    assert st is not None and st.harness == "codex"  # …but state keeps codex

    # And a plain enter afterwards still launches the recorded harness.
    res = _run(["enter", "qux", *base, "--print"])
    assert "codex" in res.stdout and "claude" not in res.stdout


def test_enter_always_refreshes_runner_state(repo: Path, root: str, hermetic_config):
    """provision.enter runs runner.refresh even when deps are unchanged: the
    docker runner's credential copies must not ride the lockfile-hash cache
    that gates setup — host logins/logouts propagate on every entry."""
    from treebox import provision
    from treebox.config import Config
    from treebox.output import Reporter

    _run(["create", "fresh-creds", "--repo", str(repo), "--root", root, "--print"])
    calls: list[str] = []

    class _Recorder:
        name = "host"

        def preflight(self, reporter):
            return None

        def facts(self):
            from treebox.runners import RunnerFacts

            return RunnerFacts(preflight_detail="", login_required=True)

        def setup(self, wt, *, cold, reporter):
            calls.append("setup")

        def refresh(self, wt, *, reporter):
            calls.append("refresh")

        def dry_run_setup(self, wt):
            return []

        def entry_command(self, wt, *, harness, args):
            return ["true"]

        def launch(self, wt, *, harness, args):
            return 0

        def teardown(self, wt, *, reporter):
            return None

    from treebox.harnesses import get_harness

    provision.enter(
        Config(root=root),
        _Recorder(),
        repo=str(repo),
        name="fresh-creds",
        harness=get_harness("claude"),
        cold=False,
        args=[],
        reporter=Reporter(quiet=True),
    )
    assert calls == ["refresh"]  # deps unchanged: setup skipped, refresh still ran


def test_enter_finishes_unprovisioned_state_even_when_lockfile_hash_matches(
    repo: Path, root: str, hermetic_config
):
    """A setup crash records provisioned=False before dependencies finish.
    enter must treat that as unfinished work even when the current lockfile
    hash matches the recorded hash (notably "" for repos without lockfiles)."""
    from treebox import ecosystems

    base = ["--repo", str(repo), "--root", root]
    assert _run(["create", "half-built", *base, "--print"]).exit_code == 0
    wt = Path(root) / "half-built"
    prior = state.load(wt)
    assert prior is not None

    (wt / "uv.lock").unlink()
    (wt / "setup.log").unlink()
    current = ecosystems.lockfile_hash(wt)
    assert current == ""
    state.save(
        wt,
        state.WorktreeState(
            base=prior.base,
            isolation=prior.isolation,
            harness=prior.harness,
            lockfile_hash=current,
            provisioned=False,
            firewall=prior.firewall,
            template=prior.template,
        ),
    )

    res = _run(["enter", "half-built", *base, "--print"])
    assert res.exit_code == 0, res.output
    assert "setup never completed" in res.stderr
    assert (wt / "setup.log").read_text().strip() == "ran"
    st = state.load(wt)
    assert st is not None
    assert st.provisioned is True
    assert st.lockfile_hash == current


def test_cold_create(repo: Path, root: str, hermetic_config):
    res = _run(["create", "cold-x", "--repo", str(repo), "--root", root, "--cold", "--print"])
    assert res.exit_code == 0, res.output
    assert (Path(root) / "cold-x" / "setup.log").read_text().strip() == "ran"


def test_print_host_command_is_self_contained(
    repo: Path, root: str, hermetic_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The --print contract: a copy-pasteable launch command. For host isolation
    it must carry the worktree directory — replayed from the repo root (or
    anywhere), the agent starts in the box, never in the main checkout."""
    import os

    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\npwd\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    res = _run(["create", "boxed", "--repo", str(repo), "--root", root, "--print"])
    assert res.exit_code == 0, res.output
    printed = res.stdout.strip()
    wt = Path(root) / "boxed"
    assert str(wt) in printed and "claude" in printed

    # Replay the printed command from the MAIN checkout — the worst place to
    # accidentally launch a full-autonomy agent.
    replay = subprocess.run(printed, shell=True, capture_output=True, text=True, cwd=str(repo))
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout.strip() == str(wt)  # the fake agent ran in the worktree


def test_firewall_flag_is_tristate(
    repo: Path, root: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """--no-firewall must opt a single run out of a config-enabled firewall,
    and --firewall must opt in over a config default of false — the same
    both-directions override every other config-backed option already has."""
    cfg = tmp_path / "fw-config.toml"
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    base = ["--repo", str(repo), "--root", root, "--dry-run", "--json"]

    cfg.write_text('isolation = "docker"\nfirewall = true\n')
    res = _run(["create", "fw", *base])
    assert res.exit_code == 0, res.output
    assert "init-firewall.sh" in res.stdout  # config default applies

    res = _run(["create", "fw", *base, "--no-firewall"])
    assert res.exit_code == 0, res.output
    assert "init-firewall.sh" not in res.stdout  # explicit opt-out wins

    cfg.write_text('isolation = "docker"\nfirewall = false\n')
    res = _run(["create", "fw", *base, "--firewall"])
    assert res.exit_code == 0, res.output
    assert "init-firewall.sh" in res.stdout  # explicit opt-in wins


def test_list_and_teardown(repo: Path, root: str, hermetic_config):
    _run(
        [
            "create",
            "feature-auth",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--print",
        ]
    )
    payload = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
    assert payload["schemaVersion"] == SCHEMA_VERSION
    # `ls` is a hidden alias with identical behavior.
    alias = json.loads(_run(["ls", "--repo", str(repo), "--root", root, "--json"]).stdout)
    assert alias == payload
    rows = payload["worktrees"]
    row = next(r for r in rows if r["name"] == "feature-auth")
    # Identity + live branch (the explicit name, not a placeholder) + recency.
    assert row["branch"] == "feature-auth"
    assert row["unnamed"] is False
    assert row["deps"] == "fresh"
    assert row["isolation"] == "host"
    assert row["harness"] == "claude"
    assert row["last_commit"] and row["commit_epoch"] > 0

    res = _run(["teardown", "feature-auth", "--repo", str(repo), "--root", root, "--force"])
    assert res.exit_code == 0, res.output
    assert not (Path(root) / "feature-auth").exists()


def test_list_shows_renamed_branch_live_and_named(repo: Path, root: str, hermetic_config):
    _run(["create", "wip", "--repo", str(repo), "--root", root, "--print"])
    assert _git(Path(root) / "wip", "branch", "-m", "real-work").returncode == 0
    payload = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
    row = next(r for r in payload["worktrees"] if r["name"] == "wip")
    assert row["branch"] == "real-work"
    assert row["unnamed"] is False


def test_list_sorts_by_recency(repo: Path, root: str, hermetic_config):
    _run(["create", "older", "--repo", str(repo), "--root", root, "--print"])
    _run(["create", "newer", "--repo", str(repo), "--root", root, "--print"])
    wt = Path(root) / "newer"
    (wt / "work.txt").write_text("x\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
        "GIT_COMMITTER_DATE": "2035-01-01T00:00:00",
        "GIT_AUTHOR_DATE": "2035-01-01T00:00:00",
    }
    import os

    subprocess.run(["git", "-C", str(wt), "add", "work.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-q", "-m", "newest work"],
        check=True,
        env={**os.environ, **env},
    )
    payload = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
    assert [r["name"] for r in payload["worktrees"]] == ["newer", "older"]
    assert payload["worktrees"][0]["last_commit"] == "newest work"


def test_list_reports_stale_and_unknown_deps(repo: Path, root: str, hermetic_config):
    """The deps column's other two states: 'stale' when the recorded lockfile
    hash no longer matches the tree, 'unknown' when no state was recorded."""
    wt = Path(root) / "feature-auth"
    _run(
        [
            "create",
            "feature-auth",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--print",
        ]
    )

    def deps() -> str:
        payload = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
        return next(r["deps"] for r in payload["worktrees"] if r["name"] == "feature-auth")

    # Deps landed on the branch: recorded hash no longer matches → stale.
    (wt / "uv.lock").write_text("version = 2\n")
    assert deps() == "stale"

    # No recorded state at all (provisioning died before setup) → unknown.
    state._state_path(wt).unlink()
    assert deps() == "unknown"


def test_teardown_variadic_and_by_branch(repo: Path, root: str, hermetic_config):
    """teardown takes several refs — each a name, branch, or substring — and
    removes them all; the plan is resolved up front so one typo removes nothing."""
    for name in ("alpha", "feature/beta", "gamma"):
        _run(["create", name, "--repo", str(repo), "--root", root, "--print"])

    # A typo among the refs must remove NOTHING (all-or-nothing resolution).
    res = _run(
        ["teardown", "alpha", "nope-nothing", "--repo", str(repo), "--root", root, "--force"]
    )
    assert res.exit_code == 3
    assert (Path(root) / "alpha").is_dir()

    res = _run(
        [
            "teardown",
            "alpha",
            "feature/beta",  # by branch (its directory is feature--beta)
            "--repo",
            str(repo),
            "--root",
            root,
            "--force",
            "--json",
        ]
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["schemaVersion"] == SCHEMA_VERSION
    assert [w["name"] for w in payload["worktrees"]] == ["alpha", "feature--beta"]
    assert all(w["removed"] for w in payload["worktrees"])
    assert not (Path(root) / "alpha").exists() and not (Path(root) / "feature--beta").exists()
    assert (Path(root) / "gamma").is_dir()  # untouched


def test_teardown_delete_branch(repo: Path, root: str, hermetic_config):
    from treebox import git

    _run(
        [
            "create",
            "feature-gone",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--print",
        ]
    )
    assert git.local_branch_exists(str(repo), "feature-gone")
    _run(
        [
            "teardown",
            "feature-gone",
            "--repo",
            str(repo),
            "--root",
            root,
            "--force",
            "--delete-branch",
        ]
    )
    assert not git.local_branch_exists(str(repo), "feature-gone")


def test_fetch_is_required_and_fails_loudly(repo: Path, root: str, hermetic_config):
    # Break origin so no fetch can succeed.
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "/nonexistent/x.git"],
        check=True,
    )
    # Default create must fail LOUDLY (exit 4), never silently use stale refs.
    res = _run(["create", "fresh-x", "--repo", str(repo), "--root", root])
    assert res.exit_code == 4  # EXIT_PERMISSION
    assert not (Path(root) / "fresh-x").exists()
    # --no-fetch is the explicit opt-out and succeeds against local refs.
    ok = _run(
        [
            "create",
            "fresh-x",
            "--repo",
            str(repo),
            "--root",
            root,
            "--no-fetch",
            "--print",
        ]
    )
    assert ok.exit_code == 0


def test_fetch_failure_is_structured_in_json(repo: Path, root: str, hermetic_config):
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "/nonexistent/x.git"],
        check=True,
    )
    res = _run(["create", "fresh-y", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 4
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "FETCH_FAILED" and "hint" in err["error"]


def test_doctor_reports_unreachable_origin(repo: Path, root: str, hermetic_config):
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "/nonexistent/x.git"],
        check=True,
    )
    doc = json.loads(_run(["doctor", "--repo", str(repo), "--json"]).stdout)
    auth = next(c for c in doc["checks"] if c["name"] == "git auth")
    assert auth["ok"] is False
    assert doc["advisories"]


def test_enter_missing_is_not_found(repo: Path, root: str, hermetic_config):
    res = _run(["enter", "ghost-x", "--repo", str(repo), "--root", root])
    assert res.exit_code == 3  # EXIT_NOTFOUND


def test_enter_missing_json_error(repo: Path, root: str, hermetic_config):
    res = _run(["enter", "ghost-x", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 3
    err = json.loads(res.stderr)
    assert err["schemaVersion"] == SCHEMA_VERSION
    assert err["error"]["code"] == "NOT_FOUND"
    assert "hint" in err["error"]


def test_teardown_dirty_is_conflict(repo: Path, root: str, hermetic_config):
    wt = Path(root) / "dirty-x"
    _run(
        [
            "create",
            "dirty-x",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--print",
        ]
    )
    (wt / "uncommitted.txt").write_text("x")
    res = _run(["teardown", "dirty-x", "--repo", str(repo), "--root", root])
    assert res.exit_code == 5  # EXIT_CONFLICT


def test_teardown_clean_needs_confirmation_non_interactively(
    repo: Path, root: str, hermetic_config
):
    """A clean teardown without --force under a non-TTY (CliRunner's stdin)
    must refuse with exit 5 (NEEDS_CONFIRMATION), not prompt or delete."""
    wt = Path(root) / "confirm-x"
    _run(
        [
            "create",
            "confirm-x",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--print",
        ]
    )
    res = _run(["teardown", "confirm-x", "--repo", str(repo), "--root", root])
    assert res.exit_code == 5  # EXIT_CONFLICT (NEEDS_CONFIRMATION)
    assert "--force" in res.stderr  # the hint names the escape hatch
    assert wt.is_dir()  # nothing was removed


def test_lock_held_is_conflict(repo: Path, root: str, hermetic_config):
    """A create racing a run that holds the worktree lock must exit 5 with the
    structured LOCK_HELD error, so scripted callers can branch on it."""
    from treebox import locking

    with locking.worktree_lock(str(repo), root, "locked-x"):
        res = _run(["create", "locked-x", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5  # EXIT_CONFLICT
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "LOCK_HELD"
    assert "hint" in err["error"]
    assert not (Path(root) / "locked-x").exists()  # nothing was provisioned


def test_dry_run_changes_nothing(repo: Path, root: str, hermetic_config):
    wt = Path(root) / "planned-x"
    res = _run(
        [
            "create",
            "planned-x",
            "--repo",
            str(repo),
            "--root",
            root,
            "--base",
            "dev",
            "--dry-run",
            "--json",
        ]
    )
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["schemaVersion"] == SCHEMA_VERSION and payload["dry_run"] is True
    assert payload["name"] == "planned-x"
    assert payload["branch"] == "planned-x"
    assert any("worktree add" in c for c in payload["commands"])
    assert any("pre-push guard" in c for c in payload["commands"])
    assert not wt.exists()  # nothing created
    from treebox import git

    assert not git.local_branch_exists(str(repo), "planned-x")


def test_create_and_doctor_json_have_schema_version(repo: Path, root: str, hermetic_config):
    created = json.loads(
        _run(["create", "sv", "--repo", str(repo), "--root", root, "--json"]).stdout
    )
    assert created["schemaVersion"] == SCHEMA_VERSION
    assert created["name"] == "sv"
    doc = json.loads(_run(["doctor", "--repo", str(repo), "--json"]).stdout)
    assert doc["schemaVersion"] == SCHEMA_VERSION


def _rewrite_state_runner(wt: Path, runner_name: str) -> None:
    """Simulate a worktree provisioned with another runner (e.g. docker)."""
    import dataclasses

    st = state.load(wt)
    assert st is not None
    state.save(wt, dataclasses.replace(st, isolation=runner_name))


def test_enter_prefers_recorded_runner(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """enter without --isolation must use the isolation mode the worktree was provisioned
    with, not the config default (host) — otherwise a docker-sandboxed worktree
    is silently entered outside its sandbox."""
    from treebox.runners import docker as dr

    wt = Path(root) / "feature-auth"
    _run(["create", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    _rewrite_state_runner(wt, "docker")

    # Docker present and healthy so enter's preflight passes on hosts without
    # a running daemon (e.g. macOS CI) — this test is about runner selection.
    monkeypatch.setattr(dr.system, "have", lambda c: True)
    monkeypatch.setattr(dr, "_docker_available", lambda: True)

    res = _run(["enter", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    assert res.exit_code == 0, res.output
    assert res.stdout.split()[0] == "docker"  # not the host launch command


def test_enter_unknown_recorded_runner_is_a_loud_error(repo: Path, root: str, hermetic_config):
    """A worktree recorded with a runner this version doesn't know is a loud
    conflict — never silently entered with the host default."""
    wt = Path(root) / "feature-auth"
    _run(["create", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    _rewrite_state_runner(wt, "bogus")

    res = _run(["enter", "feature-auth", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5, res.output  # EXIT_CONFLICT
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "UNKNOWN_ISOLATION"


def test_enter_explicit_runner_mismatch_is_conflict(repo: Path, root: str, hermetic_config):
    wt = Path(root) / "feature-auth"
    _run(["create", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    _rewrite_state_runner(wt, "docker")

    res = _run(
        [
            "enter",
            "feature-auth",
            "--repo",
            str(repo),
            "--root",
            root,
            "--isolation",
            "host",
            "--json",
        ]
    )
    assert res.exit_code == 5  # EXIT_CONFLICT
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "ISOLATION_MISMATCH"


def test_enter_preflights_the_runner_so_a_stopped_daemon_is_not_a_lost_worktree(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """A stopped Docker daemon must surface on `enter` as the same clean
    DOCKER_UNAVAILABLE error create/doctor give — never the misleading "no
    container, teardown & re-create" that would steer the user into deleting a
    perfectly good worktree over a daemon that's merely down."""
    from treebox.runners import docker as dr

    wt = Path(root) / "feature-auth"
    _run(["create", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    _rewrite_state_runner(wt, "docker")

    # docker on PATH but daemon unreachable — exactly the reported repro.
    monkeypatch.setattr(dr.system, "have", lambda c: True)
    monkeypatch.setattr(dr, "_docker_available", lambda: False)

    res = _run(["enter", "feature-auth", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 1  # EXIT_ERROR — stable; agents branch on the code
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "DOCKER_UNAVAILABLE"
    assert "docker info" in err["error"]["hint"]
    assert wt.is_dir()  # the worktree survives — no destructive teardown advice


def test_runner_recorded_before_setup_survives_a_failed_create(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """The un-tearable-orphan fix: if setup dies (a docker build/run can leave a
    container behind), the runner must already be on disk so teardown knows what
    to clean. State is written before setup with provisioned=False."""
    from treebox import state
    from treebox.runners.host import HostRunner

    def boom(self, wt, *, cold, reporter):
        raise RuntimeError("setup exploded")

    monkeypatch.setattr(HostRunner, "setup", boom)
    res = _run(["create", "crashed", "--repo", str(repo), "--root", root, "--print"])
    assert res.exit_code != 0  # the create failed…

    wt = Path(root) / "crashed"
    assert wt.is_dir()  # …but left the worktree and, crucially, its state
    st = state.load(wt)
    assert st is not None
    assert st.isolation == "host"  # recorded before setup, not lost to the crash
    assert st.provisioned is False  # setup never completed


def test_failed_create_can_be_finished_by_recreating(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """A half-built tree (provisioned=False) is not a slug conflict: re-running
    create finishes setup instead of refusing, so a transient setup failure is
    recoverable rather than a dead worktree you must delete by hand."""
    from treebox import state
    from treebox.runners.host import HostRunner

    real_setup = HostRunner.setup
    calls = {"n": 0}

    def flaky(self, wt, *, cold, reporter):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("setup exploded once")
        return real_setup(self, wt, cold=cold, reporter=reporter)

    monkeypatch.setattr(HostRunner, "setup", flaky)

    first = _run(["create", "flaky", "--repo", str(repo), "--root", root, "--print"])
    assert first.exit_code != 0
    assert state.load(Path(root) / "flaky").provisioned is False

    # Second create on the same name recovers (finishes setup) rather than 5-ing.
    second = _run(["create", "flaky", "--repo", str(repo), "--root", root, "--print"])
    assert second.exit_code == 0, second.output
    assert "unprovisioned" in second.stderr  # took the finish-setup path
    assert state.load(Path(root) / "flaky").provisioned is True


def test_provisioned_tree_is_still_a_slug_conflict(repo: Path, root: str, hermetic_config):
    """The recovery path must not weaken the conflict guard: a fully provisioned
    worktree still refuses to be clobbered by a same-name create."""
    _run(["create", "taken", "--repo", str(repo), "--root", root, "--print"])
    res = _run(["create", "taken", "--repo", str(repo), "--root", root, "--print", "--json"])
    assert res.exit_code == 5
    assert json.loads(res.stderr)["error"]["code"] == "SLUG_CONFLICT"


def test_teardown_uses_recorded_runner(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """teardown without --isolation must tear down via the recorded isolation mode, so a
    docker-sandboxed worktree's container/image/config dir are not leaked."""
    from treebox.runners.docker import DockerRunner

    wt = Path(root) / "feature-auth"
    _run(["create", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    _rewrite_state_runner(wt, "docker")

    calls: list[str] = []
    monkeypatch.setattr(
        DockerRunner,
        "teardown",
        lambda self, wt, *, reporter: calls.append(wt.name),
    )
    res = _run(["teardown", "feature-auth", "--repo", str(repo), "--root", root, "--force"])
    assert res.exit_code == 0, res.output
    assert calls == ["feature-auth"]  # DockerRunner.teardown was invoked
    assert not wt.exists()


def test_teardown_explicit_runner_mismatch_is_conflict(repo: Path, root: str, hermetic_config):
    wt = Path(root) / "feature-auth"
    _run(["create", "feature-auth", "--repo", str(repo), "--root", root, "--print"])
    _rewrite_state_runner(wt, "docker")

    res = _run(
        [
            "teardown",
            "feature-auth",
            "--repo",
            str(repo),
            "--root",
            root,
            "--force",
            "--isolation",
            "host",
        ]
    )
    assert res.exit_code == 5  # EXIT_CONFLICT
    assert wt.exists()  # nothing was removed


def test_teardown_batch_isolation_mismatch_is_all_or_nothing(
    repo: Path, root: str, hermetic_config
):
    """An --isolation mismatch on the SECOND target must abort the batch before
    anything is removed: previously it fired mid-loop, so the first worktree
    was gone but the --json payload carried only the error object — a scripting
    agent reasonably concluded nothing was removed."""
    base = ["--repo", str(repo), "--root", root]
    _run(["create", "first", *base, "--print"])
    _run(["create", "second", *base, "--print"])
    _rewrite_state_runner(Path(root) / "second", "docker")

    res = _run(["teardown", "first", "second", *base, "--force", "--isolation", "host", "--json"])
    assert res.exit_code == 5  # EXIT_CONFLICT
    assert json.loads(res.stderr)["error"]["code"] == "ISOLATION_MISMATCH"
    # All-or-nothing: the matching first target must NOT have been removed.
    assert (Path(root) / "first").exists()
    assert (Path(root) / "second").exists()


def test_teardown_lock_held_is_conflict(repo: Path, root: str, hermetic_config):
    """teardown takes the same per-name lock create/enter hold: a batch naming
    a locked worktree exits 5 LOCK_HELD before removing anything — it must not
    delete a tree out from under a concurrent provision."""
    from treebox import locking

    base = ["--repo", str(repo), "--root", root]
    _run(["create", "free", *base, "--print"])
    _run(["create", "busy", *base, "--print"])

    with locking.worktree_lock(str(repo), root, "busy"):
        res = _run(["teardown", "free", "busy", *base, "--force", "--json"])
    assert res.exit_code == 5  # EXIT_CONFLICT
    assert json.loads(res.stderr)["error"]["code"] == "LOCK_HELD"
    # Locks are all taken before any removal: both targets survive.
    assert (Path(root) / "free").exists()
    assert (Path(root) / "busy").exists()

    # With the lock released the same batch goes through.
    res = _run(["teardown", "free", "busy", *base, "--force", "--json"])
    assert res.exit_code == 0, res.output
    assert not (Path(root) / "free").exists()
    assert not (Path(root) / "busy").exists()


def test_create_reuses_a_leftover_placeholder_branch(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """teardown keeps the branch by default; re-creating the same generated
    name must reuse that treebox/<name> branch (with the guard re-installed),
    not fail. Placeholders only — a kept *named* branch is a BRANCH_EXISTS
    conflict instead."""
    from treebox import git, names

    monkeypatch.setattr(names, "petname", lambda taken: "revive")
    base = ["--repo", str(repo), "--root", root]
    _run(["create", *base, "--print"])
    _run(["teardown", "revive", *base, "--force"])
    assert git.local_branch_exists(str(repo), "treebox/revive")

    res = _run(["create", *base, "--print"])
    assert res.exit_code == 0, res.output
    wt = Path(root) / "revive"
    assert git.branch_for_path(str(repo), str(wt)) == "treebox/revive"
    assert _git(wt, "config", "--worktree", "core.hooksPath").returncode == 0


def test_create_refuses_stray_unregistered_dir(repo: Path, root: str, hermetic_config):
    """A leftover directory at the worktree path that git does NOT know as a
    worktree (e.g. a partial rmtree during teardown) must be a loud conflict —
    'finishing setup' there would resolve git against the MAIN repo: hooksPath
    rewritten, state dropped into the main .git, agent launched on the user's
    real checkout."""
    stray = Path(root) / "bar"
    stray.mkdir(parents=True)

    res = _run(["create", "bar", "--repo", str(repo), "--root", root, "--print", "--json"])
    assert res.exit_code == 5, res.output
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "SLUG_CONFLICT"
    assert "not a healthy registered worktree" in err["error"]["hint"]

    # The main repo was never touched: no worktree-config extension, no
    # hooksPath hijack, no stray treebox state, and no worktree registered.
    assert _git(repo, "config", "extensions.worktreeConfig").returncode != 0
    assert not (repo / ".git" / "config.worktree").exists()
    assert not (repo / ".git" / "treebox-state.json").exists()
    from treebox import git

    assert git.branch_for_path(str(repo), str(stray)) is None


def test_create_refuses_registered_worktree_missing_git_pointer(
    repo: Path, root: str, hermetic_config
):
    """An interrupted teardown can rm the worktree's .git pointer file while
    leaving both the directory and git's main-side registration intact. The dir
    still reports the expected branch, so the resume guard must NOT 'finish
    setup': git commands inside a pointer-less dir resolve to the MAIN repo,
    which would rewrite the real checkout's hooksPath and drop state there."""
    from treebox import git

    base = ["--repo", str(repo), "--root", root]
    _run(["create", "orphan", *base, "--print"])
    wt = Path(root) / "orphan"
    assert git.branch_for_path(str(repo), str(wt)) == "orphan"

    # Simulate the interrupted teardown: pointer gone, registration remains.
    (wt / ".git").unlink()
    assert git.branch_for_path(str(repo), str(wt)) == "orphan"

    res = _run(["create", "orphan", *base, "--print", "--json"])
    assert res.exit_code == 5, res.output
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "SLUG_CONFLICT"
    assert "not a healthy registered worktree" in err["error"]["hint"]

    # The hijack never happened: no per-worktree config or hooksPath dropped
    # into the main .git root, and no treebox state leaked there.
    assert _git(repo, "config", "core.hooksPath").returncode != 0
    assert not (repo / ".git" / "config.worktree").exists()
    assert not (repo / ".git" / "treebox-state.json").exists()


def test_create_refuse_hint_points_to_teardown_and_prune(repo: Path, root: str, hermetic_config):
    """A bare rm -rf leaves git's registration behind, so the refuse hint must
    steer to a recovery that clears BOTH: `treebox teardown <name>`, or a manual
    rm -rf paired with `git worktree prune` — never a bare rm -rf that dead-ends
    the next create."""
    stray = Path(root) / "leftover"
    stray.mkdir(parents=True)

    res = _run(["create", "leftover", "--repo", str(repo), "--root", root, "--print", "--json"])
    assert res.exit_code == 5, res.output
    hint = json.loads(res.stderr)["error"]["hint"]
    assert "treebox teardown leftover" in hint
    assert "worktree prune" in hint


def test_create_self_heals_stale_registration_from_interrupted_teardown(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """An interrupted teardown can remove the working dir while leaving git's
    per-worktree registration (git flags it prunable) and the placeholder branch
    behind. The next create must not dead-end on `git branch -f` / `worktree add`
    against that corpse — it prunes the stale registration and succeeds."""
    import shutil

    from treebox import git, names

    monkeypatch.setattr(names, "petname", lambda taken: "revive-stale")
    base = ["--repo", str(repo), "--root", root]
    _run(["create", *base, "--print"])
    wt = Path(root) / "revive-stale"
    shutil.rmtree(wt)  # dir gone, registration + placeholder branch remain
    assert any(
        r.prunable and Path(r.path).name == "revive-stale" for r in git.worktree_list(str(repo))
    )
    assert git.local_branch_exists(str(repo), "treebox/revive-stale")

    res = _run(["create", *base, "--print"])
    assert res.exit_code == 0, res.output
    assert git.branch_for_path(str(repo), str(wt)) == "treebox/revive-stale"
    assert _git(wt, "config", "--worktree", "core.hooksPath").returncode == 0
    # No leftover corpse: exactly one live registration at the path, not prunable.
    recs = [r for r in git.worktree_list(str(repo)) if r.path == str(wt)]
    assert recs == [git.WorktreeRecord(str(wt), "treebox/revive-stale", False)]


def test_create_resets_stale_placeholder_to_fresh_base(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """A kept placeholder branch whose tip is contained in origin/<base> must be
    fast-forwarded to the fresh base on re-create — never silently hand the
    agent a weeks-old tip while the summary claims freshness."""
    from treebox import git, names

    monkeypatch.setattr(names, "petname", lambda taken: "stale")
    base = ["--repo", str(repo), "--root", root]
    _run(["create", *base, "--print"])
    _run(["teardown", "stale", *base, "--force"])
    assert git.local_branch_exists(str(repo), "treebox/stale")

    # origin/main moves on while the placeholder lingers.
    (repo / "new.txt").write_text("new\n")
    _git(repo, "add", "new.txt")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "advance")
    _git(repo, "push", "origin", "main")
    fresh_tip = _git(repo, "rev-parse", "origin/main").stdout.strip()

    res = _run(["create", *base, "--print"])
    assert res.exit_code == 0, res.output
    wt = Path(root) / "stale"
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == fresh_tip


def test_create_warns_on_diverged_placeholder(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """A placeholder with unpushed commits must be resumed (never destroyed),
    but loudly: the tree is that old work, not a fresh origin/<base>."""
    from treebox import names

    monkeypatch.setattr(names, "petname", lambda taken: "diverged")
    base = ["--repo", str(repo), "--root", root]
    _run(["create", *base, "--print"])
    wt = Path(root) / "diverged"
    (wt / "scratch.txt").write_text("wip\n")
    _git(wt, "add", "scratch.txt")
    _git(wt, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "wip")
    old_tip = _git(wt, "rev-parse", "HEAD").stdout.strip()
    _run(["teardown", "diverged", *base, "--force"])

    # origin/main moves on; the placeholder's commit is not on it.
    (repo / "other.txt").write_text("x\n")
    _git(repo, "add", "other.txt")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "advance")
    _git(repo, "push", "origin", "main")

    res = _run(["create", *base, "--print"])
    assert res.exit_code == 0, res.output
    assert "commits not on origin/main" in res.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == old_tip


def test_worktree_root_is_excluded_in_target_repo(repo: Path, hermetic_config):
    """A repo-relative worktree root must be force-ignored via info/exclude —
    no permanent '?? .treebox/' noise, and a `git add -A` in the main checkout
    must not stage provisioned worktrees wholesale."""
    res = _run(["create", "tidy", "--repo", str(repo), "--root", ".treebox/worktrees", "--print"])
    assert res.exit_code == 0, res.output

    porcelain = _git(repo, "status", "--porcelain").stdout
    assert ".treebox" not in porcelain, porcelain
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert "/.treebox/worktrees/" in exclude.splitlines()

    # Only the full root is excluded — untracked siblings under the same
    # top-level directory must stay visible to git.
    (repo / ".treebox" / "notes.txt").write_text("keep me visible\n")
    porcelain = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    assert ".treebox/notes.txt" in porcelain, porcelain
    assert ".treebox/worktrees" not in porcelain, porcelain

    # Idempotent: a second create must not duplicate the entry.
    _run(["create", "tidy-two", "--repo", str(repo), "--root", ".treebox/worktrees", "--print"])
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count("/.treebox/worktrees/") == 1


def test_checkout_not_found_hint_names_the_real_flag(repo: Path, root: str, hermetic_config):
    """Agents follow error hints verbatim: the NOT_FOUND hint must name
    --checkout, not the removed -b flag."""
    res = _run(
        ["create", "--checkout", "ghost-branch", "--repo", str(repo), "--root", root, "--json"]
    )
    assert res.exit_code == 3
    hint = json.loads(res.stderr)["error"]["hint"]
    assert "--checkout" in hint
    assert "drop -b" not in hint


def test_enter_miss_on_existing_branch_hints_checkout(repo: Path, root: str, hermetic_config):
    """`enter <ref>` where <ref> is a real branch with no worktree must point
    at the command that materializes one, not the generic miss advice."""
    subprocess.run(["git", "-C", str(repo), "branch", "orphaned-branch"], check=True)
    res = _run(["enter", "orphaned-branch", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 3
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "NOT_FOUND"
    assert "treebox create --checkout orphaned-branch" in err["hint"]

    # A ref that matches nothing keeps the generic advice.
    res = _run(["enter", "utter-ghost", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 3
    hint = json.loads(res.stderr)["error"]["hint"]
    assert "--checkout" not in hint


# --- teardown chooser ---------------------------------------------------------


def test_branch_status_helpers(repo: Path, root: str, hermetic_config):
    """The Tier-1 git signals the chooser renders: forge-agnostic, local-only."""
    from treebox import git

    # A local-path origin can't be attributed to a forge -> Tier-1 only.
    assert git.origin_host(str(repo)) is None

    _run(["create", "feat-x", "--repo", str(repo), "--root", root, "--print"])
    wt = str(Path(root) / "feat-x")

    # Branching from origin/main auto-tracks it, level and merged (an ancestor).
    assert git.upstream_of(wt) == "origin/main"
    assert git.ahead_behind(wt) == (0, 0)
    assert git.is_merged_into(str(repo), "feat-x", "main") is True

    # A new commit puts it ahead of, and off, origin/main. Stage just the file:
    # the copied submodule has no linkage, so a blanket `add -A` would choke.
    (Path(wt) / "new.txt").write_text("x")
    _git(wt, "add", "new.txt")
    _git(wt, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "work", "--", "new.txt")
    assert git.ahead_behind(wt) == (1, 0)
    assert git.is_merged_into(str(repo), "feat-x", "main") is False


def test_teardown_no_refs_refuses_without_tty(repo: Path, root: str, hermetic_config):
    """Bare `teardown` under a non-TTY (CliRunner stdin) must refuse, not hang on
    a picker — same contract as the confirm prompt."""
    _run(["create", "solo", "--repo", str(repo), "--root", root, "--print"])
    res = _run(["teardown", "--repo", str(repo), "--root", root])
    assert res.exit_code == 5
    assert (Path(root) / "solo").is_dir()


def test_teardown_no_refs_json_refuses(repo: Path, root: str, hermetic_config):
    """Bare `teardown --json` never reaches the chooser: structured refusal, and
    nothing leaks to stdout."""
    _run(["create", "solo", "--repo", str(repo), "--root", root, "--print"])
    res = _run(["teardown", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "NEEDS_CONFIRMATION"
    assert res.stdout == ""


def test_teardown_chooser_selects_and_removes(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """The chooser removes exactly what was picked, and its selection stands in
    for the confirm prompt (no --force needed for a clean worktree)."""
    from treebox import cli

    for name in ("alpha", "beta"):
        _run(["create", name, "--repo", str(repo), "--root", root, "--print"])
        (Path(root) / name / "setup.log").unlink()  # drop the untracked marker

    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(cli, "_choose_branches_to_delete", lambda r, e, chosen, *, default: set())

    def pick_alpha(reporter, entries):
        return [c for (c, _p, _s) in entries if c.name == "alpha"]

    monkeypatch.setattr(cli, "_prompt_selection", pick_alpha)
    res = _run(["teardown", "--repo", str(repo), "--root", root])
    assert res.exit_code == 0, res.output
    assert not (Path(root) / "alpha").is_dir()  # picked -> removed
    assert (Path(root) / "beta").is_dir()  # not picked -> untouched


def test_teardown_chooser_dirty_is_skipped_not_fatal(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """A single dirty pick is kept (not removed) and exits conflict — the dirty
    gate still holds, it just no longer needs --force to *stop*."""
    from treebox import cli

    _run(["create", "dd", "--repo", str(repo), "--root", root, "--print"])
    # The setup hook's setup.log marker leaves the worktree dirty; keep it.
    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(cli, "_choose_branches_to_delete", lambda r, e, chosen, *, default: set())
    monkeypatch.setattr(cli, "_prompt_selection", lambda r, entries: [c for (c, _p, _s) in entries])
    res = _run(["teardown", "--repo", str(repo), "--root", root])
    assert res.exit_code == 5  # EXIT_CONFLICT: nothing clean to remove, dirty kept
    assert (Path(root) / "dd").is_dir()
    assert "uncommitted changes" in res.stderr


def test_teardown_chooser_mixed_dirty_and_clean(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """The headline edge case: a selection mixing dirty and clean removes the
    clean ones, keeps the dirty ones, and reports both — exit conflict signals
    that not everything asked for happened."""
    from treebox import cli

    # clean-a / clean-b lose their untracked marker; dirty-c keeps it (dirty).
    for name in ("clean-a", "clean-b"):
        _run(["create", name, "--repo", str(repo), "--root", root, "--print"])
        (Path(root) / name / "setup.log").unlink()
    _run(["create", "dirty-c", "--repo", str(repo), "--root", root, "--print"])

    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(cli, "_choose_branches_to_delete", lambda r, e, chosen, *, default: set())
    monkeypatch.setattr(cli, "_prompt_selection", lambda r, entries: [c for (c, _p, _s) in entries])
    res = _run(["teardown", "--repo", str(repo), "--root", root])

    assert res.exit_code == 5  # some were skipped -> honest conflict
    assert not (Path(root) / "clean-a").is_dir()  # clean -> removed
    assert not (Path(root) / "clean-b").is_dir()  # clean -> removed
    assert (Path(root) / "dirty-c").is_dir()  # dirty -> kept
    # The recap names the kept worktree and tallies the outcome so it's legible.
    assert "dirty-c" in res.stderr
    assert "Removed 2" in res.stderr and "kept 1" in res.stderr


def test_teardown_chooser_deletes_branches_per_worktree(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """Branch deletion is per-worktree, not all-or-none: pick both worktrees but
    only one branch, and exactly that branch is dropped while the other survives."""
    from treebox import cli, git

    for name in ("keep-br", "drop-br"):
        _run(["create", name, "--repo", str(repo), "--root", root, "--print"])
        (Path(root) / name / "setup.log").unlink()
    assert git.local_branch_exists(str(repo), "keep-br")
    assert git.local_branch_exists(str(repo), "drop-br")

    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(cli, "_prompt_selection", lambda r, entries: [c for (c, _p, _s) in entries])
    # The branch picker returns only drop-br's path — keep-br's branch stays.
    monkeypatch.setattr(
        cli,
        "_choose_branches_to_delete",
        lambda r, e, chosen, *, default: {c.path for c in chosen if c.name == "drop-br"},
    )
    res = _run(["teardown", "--repo", str(repo), "--root", root])
    assert res.exit_code == 0, res.output
    assert not (Path(root) / "keep-br").is_dir() and not (Path(root) / "drop-br").is_dir()
    assert git.local_branch_exists(str(repo), "keep-br")  # branch kept
    assert not git.local_branch_exists(str(repo), "drop-br")  # branch dropped


def test_teardown_cancel_at_branch_question_removes_nothing(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """Ctrl+C on the branch picker (questionary .ask() -> None) aborts the whole
    teardown — it must not be conflated with 'enter, no branches' and proceed."""
    from treebox import cli

    _run(["create", "bail", "--repo", str(repo), "--root", root, "--print"])
    (Path(root) / "bail" / "setup.log").unlink()

    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(cli, "_prompt_selection", lambda r, entries: [c for (c, _p, _s) in entries])
    monkeypatch.setattr(cli, "_choose_branches_to_delete", lambda r, e, chosen, *, default: None)
    res = _run(["teardown", "--repo", str(repo), "--root", root])
    assert res.exit_code == 0, res.output
    assert (Path(root) / "bail").is_dir()  # nothing removed
    assert "Cancelled" in res.stderr


def test_teardown_refuses_unknown_recorded_isolation(repo: Path, root: str, hermetic_config):
    """A worktree whose recorded isolation mode treebox can't drive is a hard
    conflict on teardown, exactly as on enter: the batch aborts untouched.
    --skip-container is the escape hatch that removes it anyway."""
    from treebox import state

    for name in ("broken", "modern"):
        _run(["create", name, "--repo", str(repo), "--root", root, "--print"])

    # Rewrite one worktree's recorded isolation to a mode treebox no longer knows.
    broken_path = Path(root) / "broken"
    st = state.load(broken_path)
    assert st is not None
    st.isolation = "devcontainer"
    state.save(broken_path, st)

    # The conflict fires before any removal, so the whole batch is left untouched.
    res = _run(["teardown", "broken", "modern", "--repo", str(repo), "--root", root, "--force"])
    assert res.exit_code == 5, res.output  # EXIT_CONFLICT
    assert "unknown isolation mode" in res.stderr
    assert (Path(root) / "broken").is_dir()  # nothing removed
    assert (Path(root) / "modern").is_dir()

    # --skip-container does no container work, so it skips isolation resolution
    # and removes the otherwise-undrivable tree anyway.
    res = _run(
        ["teardown", "broken", "--repo", str(repo), "--root", root, "--force", "--skip-container"]
    )
    assert res.exit_code == 0, res.output
    assert not (Path(root) / "broken").is_dir()


def test_teardown_chooser_cancel_removes_nothing(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """An empty selection (cancel) exits cleanly and deletes nothing."""
    from treebox import cli

    _run(["create", "keep", "--repo", str(repo), "--root", root, "--print"])
    (Path(root) / "keep" / "setup.log").unlink()
    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(cli, "_prompt_selection", lambda r, entries: [])
    res = _run(["teardown", "--repo", str(repo), "--root", root])
    assert res.exit_code == 0, res.output
    assert (Path(root) / "keep").is_dir()


def test_commands_anchor_to_main_worktree(repo: Path, root: str, hermetic_config):
    """Run from inside a linked worktree and treebox still resolves the main
    repo — so list/teardown see the whole set instead of an empty one."""
    from treebox import git

    _run(["create", "anchored", "--repo", str(repo), "--root", root, "--print"])
    wt = str(Path(root) / "anchored")

    # Naive --show-toplevel from a linked worktree points at the worktree itself;
    # main_worktree corrects it back to the main repo.
    assert Path(git.repo_root(wt)).resolve() == Path(wt).resolve()
    assert Path(git.main_worktree(wt)).resolve() == Path(repo).resolve()

    # list --repo <linked worktree> sees the set (previously: empty).
    res = _run(["list", "--repo", wt, "--root", root, "--json"])
    assert res.exit_code == 0
    names = [w["name"] for w in json.loads(res.stdout)["worktrees"]]
    assert "anchored" in names

    # teardown --repo <linked worktree> resolves and removes it (was a no-op).
    (Path(wt) / "setup.log").unlink()
    res = _run(["teardown", "anchored", "--repo", wt, "--root", root, "--force"])
    assert res.exit_code == 0, res.output
    assert not Path(wt).is_dir()


# --- CLI-surface gaps (issue #131 §2) -------------------------------------------


def test_rm_alias_matches_teardown(repo: Path, root: str, hermetic_config):
    """`rm` is a hidden alias of teardown with identical behavior and payload."""
    _run(["create", "rmme", "--repo", str(repo), "--root", root, "--print"])
    res = _run(["rm", "rmme", "--repo", str(repo), "--root", root, "--force", "--json"])
    assert res.exit_code == 0, res.output
    (record,) = json.loads(res.stdout)["worktrees"]
    assert record["name"] == "rmme" and record["removed"] is True
    assert not (Path(root) / "rmme").exists()


def test_enter_passes_extra_args_to_the_harness(repo: Path, root: str, hermetic_config):
    """`enter <ref> -- <args>` appends the extra args to the launch command —
    the CLI wiring for the variadic, not just the runner-level append."""
    _run(["create", "argy", "--repo", str(repo), "--root", root, "--print"])
    res = _run(
        [
            "enter",
            "argy",
            "--repo",
            str(repo),
            "--root",
            root,
            "--print",
            "--",
            "--continue",
            "--model",
            "opus",
        ]
    )
    assert res.exit_code == 0, res.output
    assert "--continue --model opus" in res.stdout
    # And the --json entry_command carries them too.
    res = _run(["enter", "argy", "--repo", str(repo), "--root", root, "--json", "--", "--continue"])
    cmd = json.loads(res.stdout)["entry_command"]
    assert cmd[-1].endswith("--continue")


def test_create_explicit_name_with_checkout(repo: Path, root: str, hermetic_config):
    """`create NAME --checkout BRANCH`: the explicit name wins over the derived
    (flattened) one, and the exact branch is checked out."""
    from treebox import git

    subprocess.run(["git", "-C", str(repo), "branch", "feat/combo"], check=True)
    res = _run(
        [
            "create",
            "widget",
            "--checkout",
            "feat/combo",
            "--repo",
            str(repo),
            "--root",
            root,
            "--json",
        ]
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["name"] == "widget" and payload["branch"] == "feat/combo"
    assert (Path(root) / "widget").is_dir()
    assert not (Path(root) / "feat--combo").exists()  # derived name unused
    assert git.branch_for_path(str(repo), str(Path(root) / "widget")) == "feat/combo"


def test_launch_propagates_the_agent_exit_code(
    repo: Path, root: str, hermetic_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The full launch path: without --print/--json, create provisions, prints
    the Ready line, launches the harness, and exits with the AGENT's exit code."""
    import os

    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\nexit 7\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    res = _run(["create", "launchy", "--repo", str(repo), "--root", root])
    assert res.exit_code == 7, res.output  # the fake agent's exit code, verbatim
    assert "Ready" in res.stderr and "launching claude" in res.stderr


def test_launch_missing_harness_is_a_clean_error(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """A harness CLI absent from PATH must exit 1 with an instructive message,
    not a traceback."""
    from treebox.runners import host as host_mod

    monkeypatch.setattr(host_mod, "have", lambda c: False)
    res = _run(["create", "nocli", "--repo", str(repo), "--root", root])
    assert res.exit_code == 1
    assert "not found on PATH" in res.stderr
    assert "Traceback" not in res.output


def test_fetch_failed_publickey_hint_mentions_ssh(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """An SSH-auth-shaped fetch failure gets the SSH-specific remediation hint
    (ssh-agent / ssh-add), not just the generic one."""
    from treebox import git

    def boom(repo_path, *, required, interactive=False):
        raise git.FetchError("git@github.com: Permission denied (publickey).")

    monkeypatch.setattr(git, "fetch_origin", boom)
    res = _run(["create", "sshy", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 4
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "FETCH_FAILED"
    assert "ssh-add" in err["hint"] and "ssh-agent" in err["hint"]


def test_teardown_skip_container_reports_skipped(repo: Path, root: str, hermetic_config):
    _run(["create", "boxy", "--repo", str(repo), "--root", root, "--print"])
    res = _run(
        [
            "teardown",
            "boxy",
            "--repo",
            str(repo),
            "--root",
            root,
            "--force",
            "--skip-container",
            "--json",
        ]
    )
    assert res.exit_code == 0, res.output
    (record,) = json.loads(res.stdout)["worktrees"]
    assert record["container"] == "skipped"
    assert record["removed"] is True


def test_teardown_container_cleanup_failure_is_best_effort(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """Container teardown failing must not abort the removal: the worktree still
    goes, the record says container=failed, and the exit stays 0."""
    from treebox.runners.host import HostRunner

    def boom(self, wt, *, reporter):
        raise RuntimeError("daemon exploded")

    monkeypatch.setattr(HostRunner, "teardown", boom)
    _run(["create", "bestef", "--repo", str(repo), "--root", root, "--print"])
    res = _run(["teardown", "bestef", "--repo", str(repo), "--root", root, "--force", "--json"])
    assert res.exit_code == 0, res.output
    (record,) = json.loads(res.stdout)["worktrees"]
    assert record["container"] == "failed"
    assert record["removed"] is True
    assert not (Path(root) / "bestef").exists()


def test_teardown_branch_delete_failure_is_reported_not_fatal(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """git refusing the branch delete must warn and record branch_deleted=false,
    never fail the teardown that already removed the tree."""
    from treebox import git

    monkeypatch.setattr(
        git, "delete_branch", lambda repo_path, name: (_ for _ in ()).throw(git.GitError("nope"))
    )
    _run(["create", "brfail", "--repo", str(repo), "--root", root, "--print"])
    res = _run(
        [
            "teardown",
            "brfail",
            "--repo",
            str(repo),
            "--root",
            root,
            "--force",
            "--delete-branch",
            "--json",
        ]
    )
    assert res.exit_code == 0, res.output
    (record,) = json.loads(res.stdout)["worktrees"]
    assert record["removed"] is True
    assert record["branch_deleted"] is False
    assert git.local_branch_exists(str(repo), "brfail")  # branch survived


def test_teardown_ambiguous_ref_removes_nothing(repo: Path, root: str, hermetic_config):
    """An ambiguous teardown ref is a loud exit 2 and must not remove anything
    (all-or-nothing resolution, like the typo case)."""
    for name in ("amb-one", "amb-two"):
        _run(["create", name, "--repo", str(repo), "--root", root, "--print"])
    res = _run(["teardown", "amb-", "--repo", str(repo), "--root", root, "--force", "--json"])
    assert res.exit_code == 2
    assert json.loads(res.stderr)["error"]["code"] == "AMBIGUOUS_REF"
    assert (Path(root) / "amb-one").is_dir() and (Path(root) / "amb-two").is_dir()


def test_teardown_interactive_confirm(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """Explicit-ref teardown without --force on a TTY prompts once: 'n' aborts
    with nothing removed; 'y' proceeds."""
    from treebox import cli

    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    _run(["create", "asky", "--repo", str(repo), "--root", root, "--print"])
    (Path(root) / "asky" / "setup.log").unlink()  # clean, so only the prompt gates

    res = runner.invoke(app, ["teardown", "asky", "--repo", str(repo), "--root", root], input="n\n")
    assert res.exit_code == 1  # typer.confirm(abort=True) -> Aborted
    assert (Path(root) / "asky").is_dir()

    res = runner.invoke(app, ["teardown", "asky", "--repo", str(repo), "--root", root], input="y\n")
    assert res.exit_code == 0, res.output
    assert not (Path(root) / "asky").exists()


def test_doctor_human_path_reports_blocked_and_exits_1(tmp_path: Path, hermetic_config):
    """The human doctor path shares the --json exit contract: a hard-check
    failure renders a 'blocked' verdict and exits 1."""
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    res = _run(["doctor", "--repo", str(not_a_repo)])
    assert res.exit_code == 1
    assert "doctor" in res.stdout
    assert "blocked" in res.stdout
    assert "repo" in res.stdout


def test_doctor_human_path_healthy_repo_exits_0(repo: Path, hermetic_config):
    res = _run(["doctor", "--repo", str(repo)])
    assert res.exit_code == 0, res.output
    assert "doctor" in res.stdout
    assert "git auth" in res.stdout  # the slow checks rendered too
    assert "isolation: host" in res.stdout


def test_dry_run_human_rendering(repo: Path, root: str, hermetic_config):
    """The styled --dry-run plan: heading + would-run commands on stderr, no
    data on stdout, nothing created."""
    res = _run(["create", "planned-h", "--repo", str(repo), "--root", root, "--dry-run"])
    assert res.exit_code == 0, res.output
    assert res.stdout == ""  # the plan is diagnostics, not data
    assert "dry run" in res.stderr
    assert "worktree add" in res.stderr
    assert "$" in res.stderr  # command lines carry the prompt glyph
    assert not (Path(root) / "planned-h").exists()


def test_template_flag_reaches_the_docker_runner(
    repo: Path, root: str, hermetic_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """--template must plumb through to the runner: an unknown template name is
    a loud error naming it (never a silent fall-back to the default template)."""
    monkeypatch.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
    monkeypatch.setenv("TREEBOX_HOME", str(tmp_path / "home"))
    res = _run(
        [
            "create",
            "tpl",
            "--repo",
            str(repo),
            "--root",
            root,
            "--isolation",
            "docker",
            "--template",
            "ghost",
            "--dry-run",
            "--json",
        ]
    )
    assert res.exit_code == 1
    err = json.loads(res.stderr)["error"]
    assert "No template named 'ghost'" in err["message"]
    assert not (Path(root) / "tpl").exists()


def test_enter_lock_held_is_conflict(repo: Path, root: str, hermetic_config):
    """enter racing a run that holds the worktree lock exits 5 LOCK_HELD, like
    create and teardown."""
    from treebox import locking

    _run(["create", "busy-e", "--repo", str(repo), "--root", root, "--print"])
    with locking.worktree_lock(str(repo), root, "busy-e"):
        res = _run(["enter", "busy-e", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5
    assert json.loads(res.stderr)["error"]["code"] == "LOCK_HELD"


def test_list_human_table_through_the_cli(repo: Path, root: str, hermetic_config):
    """The human list path end-to-end: the table renders on stdout with the
    live rows (render_list itself is unit-tested; this pins the CLI wiring)."""
    _run(["create", "tabley", "--repo", str(repo), "--root", root, "--print"])
    res = _run(["list", "--repo", str(repo), "--root", root])
    assert res.exit_code == 0, res.output
    assert "tabley" in res.stdout
    assert "NAME" in res.stdout and "BRANCH" in res.stdout


def test_teardown_chooser_with_no_worktrees_says_so(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    from treebox import cli

    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    res = _run(["teardown", "--repo", str(repo), "--root", root])
    assert res.exit_code == 0, res.output
    assert "No worktrees to remove" in res.stderr


def test_doctor_surfaces_runner_preflight_hint_as_advisory(
    repo: Path, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """A failing isolation preflight must reach the doctor payload as a failed
    check plus its remediation hint in advisories — and exit 1 (hard check)."""
    from treebox.runners import docker as dr

    monkeypatch.setattr(dr.system, "have", lambda c: True)
    monkeypatch.setattr(dr, "_docker_available", lambda: False)
    res = _run(["doctor", "--repo", str(repo), "--isolation", "docker", "--json"])
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    check = next(c for c in payload["checks"] if c["name"] == "isolation: docker")
    assert check["ok"] is False and "daemon" in check["detail"]
    assert any("docker info" in a for a in payload["advisories"])


def test_stale_worktree_does_not_crash_and_teardown_prunes(repo: Path, root: str, hermetic_config):
    """A registration whose dir was removed behind git's back (a manual rm, or a
    stale nested tree from the old create bug) must not crash list/teardown — it
    surfaces as `missing` and teardown prunes the dangling registration."""
    import shutil

    from treebox import git

    _run(["create", "gone", "--repo", str(repo), "--root", root, "--print"])
    shutil.rmtree(Path(root) / "gone")  # remove the dir out from under git

    # git now reports it prunable...
    assert any(r.prunable and Path(r.path).name == "gone" for r in git.worktree_list(str(repo)))

    # ...and list neither crashes nor shells into it; the row is flagged missing.
    res = _run(["list", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 0, res.output
    rows = {w["name"]: w for w in json.loads(res.stdout)["worktrees"]}
    assert rows["gone"]["missing"] is True

    # teardown prunes the dangling registration (already gone -> still exit 0).
    res = _run(["teardown", "gone", "--repo", str(repo), "--root", root, "--force"])
    assert res.exit_code == 0, res.output
    assert not any(Path(r.path).name == "gone" for r in git.worktree_list(str(repo)))
