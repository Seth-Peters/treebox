"""The executable treebox scenarios.

Each function implements the scenario with the same id in
``.claude/skills/treebox-e2e/references/scenarios.md``; that file holds the
prose (command, intent, pass criterion) and this one the checks, so the two
never duplicate each other's text. ``tests/test_e2e_map.py`` keeps the ids,
the feature map and the source in step.

Every scenario runs on a fresh fixture with its group setup. Each scenario
creates the state it needs, so selecting one id never requires an earlier
scenario. Docker scenarios name their worktrees ``e2e-...`` (see
``harness.DOCKER_PREFIX``).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from harness import (
    FAKES_DIR,
    OFFLINE_TEMPLATE,
    OFFLINE_TEMPLATE_DIR,
    Background,
    Ctx,
    RunRecord,
    group,
    scenario,
    without_path_entry,
)

DOCKER = ("docker", "offline-base-image")
ESC = "\x1b["


# --- helpers -----------------------------------------------------------------------


def wt(ctx: Ctx, name: str, root: Path | None = None) -> Path:
    return (root or ctx.root) / name


def created(ctx: Ctx, name: str | None, *extra: str, root: Path | None = None) -> dict[str, Any]:
    """`create NAME --json`, checked to succeed; returns the payload."""
    args = ["create", *([name] if name else []), *ctx.wt_args(root), *extra, "--json"]
    run = ctx.tb(*args)
    if not ctx.exit_is(run, 0):
        raise AssertionError(f"create {name} failed: {run.stderr.strip()[:400]}")
    payload = ctx.json_out(run)
    return payload if isinstance(payload, dict) else {}


def listed(ctx: Ctx, root: Path | None = None) -> dict[str, dict[str, Any]]:
    run = ctx.tb("list", *ctx.wt_args(root), "--json")
    ctx.exit_is(run, 0)
    payload = ctx.json_out(run) or {}
    return {w["name"]: w for w in payload.get("worktrees", [])}


def stub_record(text: str) -> dict[str, Any] | None:
    """The stub agent's launch record, or None when it never ran."""
    match = re.search(r"TREEBOX_E2E_STUB_BEGIN\n(.*?)TREEBOX_E2E_STUB_END", text, re.S)
    if not match:
        return None
    record: dict[str, Any] = {"args": []}
    for line in match.group(1).splitlines():
        key, _, value = line.partition("=")
        if key == "arg":
            record["args"].append(value)
        else:
            record[key] = value
    return record


def worktree_registered(ctx: Ctx, path: Path) -> bool:
    out = ctx.git("worktree", "list", "--porcelain")
    return f"worktree {path}\n" in out + "\n"


def branch_exists(ctx: Ctx, branch: str) -> bool:
    return ctx.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False) != ""


def no_ansi(ctx: Ctx, run: RunRecord, what: str) -> None:
    ctx.check(ESC not in run.stdout and ESC not in run.stderr, f"{what}: no ANSI escapes")


def printed_argv(run: RunRecord) -> list[str]:
    """The --print launch command, split like a shell would."""
    return shlex.split(run.stdout.strip()) if run.stdout.strip() else []


def host_launch_argv(run: RunRecord) -> list[str]:
    """The agent argv inside a host entry command (`sh -c 'cd X && exec ...'`)."""
    argv = printed_argv(run)
    if len(argv) == 3 and argv[:2] == ["sh", "-c"]:
        inner = shlex.split(argv[2])
        if "exec" in inner:
            return inner[inner.index("exec") + 1 :]
    return []


def commit_with_index(ctx: Ctx, branch: str, edits: list[tuple[str, str, str]]) -> str:
    """Make a commit on a new branch from main without touching the working
    tree: ``edits`` are (mode, path, content) entries, mode 100644 for a file
    or 120000 for a symlink whose content is its target, or "remove" to drop
    an index entry (content unused). Pushes the branch."""
    index = ctx.sbx / f"index-{branch.replace('/', '-')}"
    env = dict(ctx.env, GIT_INDEX_FILE=str(index))
    repo = str(ctx.repo)
    ctx.sh("git", "-C", repo, "read-tree", "main", env=env)
    for mode, path, content in edits:
        if mode == "remove":
            ctx.sh("git", "-C", repo, "update-index", "--force-remove", path, env=env)
            continue
        blob = ctx.sh("git", "-C", repo, "hash-object", "-w", "--stdin", input=content).stdout
        ctx.sh(
            "git",
            "-C",
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{mode},{blob.strip()},{path}",
            env=env,
        )
    tree = ctx.sh("git", "-C", repo, "write-tree", env=env).stdout.strip()
    commit = ctx.sh(
        "git", "-C", repo, "commit-tree", tree, "-p", "main", input=f"{branch}\n"
    ).stdout.strip()
    ctx.git("branch", branch, commit)
    ctx.git("push", "-q", "origin", branch)
    index.unlink(missing_ok=True)
    return commit


# --- A. version & doctor -------------------------------------------------------------


@group("A", "version, doctor, help and completion")
def _setup_a(ctx: Ctx) -> None:
    return


@scenario("A1", "version from three spellings")
def a1(ctx: Ctx) -> None:
    runs = [ctx.tb("version"), ctx.tb("--version"), ctx.tb("-V")]
    for run in runs:
        ctx.exit_is(run, 0)
    values = {run.stdout.strip() for run in runs}
    ctx.eq(len(values), 1, "distinct version strings")
    ctx.check(bool(runs[0].stdout.strip()), "version string is not empty")


@scenario("A2", "doctor human output")
def a2(ctx: Ctx) -> None:
    run = ctx.tb("doctor", "--repo", str(ctx.repo))
    ctx.exit_is(run, 0)
    no_ansi(ctx, run, "piped doctor")
    plain = ctx.sbx / "no-env-repo"
    ctx.sh("git", "init", "-q", "-b", "main", str(plain))
    bare = ctx.tb("doctor", "--repo", str(plain))
    ctx.exit_is(bare, 0)
    text = bare.stdout + bare.stderr
    env_row = next((line for line in text.splitlines() if ".env" in line), "")
    ctx.check("optional" in env_row, f".env row says optional (row: {env_row.strip()!r})")
    ctx.check("✗" not in env_row, ".env row is not a failure mark")


@scenario("A3", "doctor --json")
def a3(ctx: Ctx) -> None:
    run = ctx.tb("doctor", "--repo", str(ctx.repo), "--json")
    ctx.exit_is(run, 0)
    d = ctx.json_out(run) or {}
    for key in ("ok", "isolation", "checks", "advisories"):
        ctx.check(key in d, f"doctor --json has {key}")
    checks = {c["name"]: c["ok"] for c in d.get("checks", [])}
    hard = ("git", "repo", "agent: claude", "login: claude")
    ctx.eq(sorted(n for n in hard if n not in checks), [], "hard check rows missing")
    ctx.eq(d.get("ok"), all(checks.get(n) for n in hard), "ok iff every hard check passed")
    ctx.eq(d.get("ok"), True, "ok (the stub agent and a fake login are present)")


@scenario("A4", "doctor --isolation docker agrees with the engine")
def a4(ctx: Ctx) -> None:
    run = ctx.tb("doctor", "--repo", str(ctx.repo), "--isolation", "docker", "--json")
    d = ctx.json_out(run) or {}
    docker = shutil.which("docker", path=ctx.env["PATH"])
    engine = (
        bool(docker)
        and subprocess.run(
            [docker or "docker", "info"], capture_output=True, env=ctx.env
        ).returncode
        == 0
    )
    if engine:
        ctx.exit_is(run, 0)
        ctx.eq(d.get("ok"), True, "ok with a reachable engine")
    else:
        ctx.exit_is(run, 1)
        row = [c for c in d.get("checks", []) if c["name"] == "isolation: docker"]
        ctx.check(bool(row) and not row[0]["ok"], "isolation: docker check fails without engine")
        ctx.note("no docker engine: A4 proved the failure report; group H is skipped")


@scenario("A5", "doctor rejects an unknown isolation")
def a5(ctx: Ctx) -> None:
    run = ctx.tb("doctor", "--isolation", "qemu")
    ctx.exit_is(run, 2)
    ctx.check("host" in run.stderr and "docker" in run.stderr, "message names host and docker")


@scenario("A6", "shell completion protocol")
def a6(ctx: Ctx) -> None:
    env = dict(ctx.env, _TREEBOX_COMPLETE="complete_bash", COMP_WORDS="treebox cre", COMP_CWORD="1")
    run = ctx.tb(env=env)
    ctx.exit_is(run, 0)
    ctx.eq(run.stdout.split(), ["create"], "completions for 'cre'")
    env = dict(env, COMP_WORDS="treebox template ", COMP_CWORD="2")
    sub = ctx.tb(env=env)
    ctx.eq(sorted(sub.stdout.split()), ["init", "list", "path"], "visible template subcommands")


@scenario("A7", "--show-completion and --install-completion", requires=("shell-detection",))
def a7(ctx: Ctx) -> None:
    show = ctx.tb_shell("bash", '"$TB" --show-completion')
    ctx.exit_is(show, 0)
    ctx.check("_TREEBOX_COMPLETE=complete_bash" in show.stdout, "bash completion script printed")
    install = ctx.tb_shell("bash", '"$TB" --install-completion')
    ctx.exit_is(install, 0)
    bashrc = ctx.fx.home / ".bashrc"
    ctx.check(bashrc.is_file() and "treebox" in bashrc.read_text(), "scratch ~/.bashrc sources it")


@scenario("A8", "help and bare invocation")
def a8(ctx: Ctx) -> None:
    run = ctx.tb("--help")
    ctx.exit_is(run, 0)
    for cmd in ("create", "enter", "list", "teardown", "template", "doctor", "version"):
        ctx.check(re.search(rf"│ {cmd}\b", run.stdout) is not None, f"--help lists {cmd}")
    for alias in ("ls", "rm"):
        ctx.check(re.search(rf"│ {alias}\b", run.stdout) is None, f"--help hides alias {alias}")
    bare = ctx.tb()
    ctx.exit_is(bare, 2)
    ctx.check("Usage" in bare.stdout + bare.stderr, "bare invocation shows usage")
    ctx.exit_is(ctx.tb("create", "--help"), 0)
    ctx.exit_is(ctx.tb("template", "init", "-h"), 0)


def doctor_rows(
    ctx: Ctx, *args: str, env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    """(exit, {row name: check}) of `doctor --json`."""
    run = ctx.tb("doctor", "--repo", str(ctx.repo), *args, "--json", env=env)
    d = ctx.json_out(run) or {}
    return run.exit, {c["name"]: c for c in d.get("checks", [])}


@scenario("A9", "doctor --harness checks that agent's binary and login")
def a9(ctx: Ctx) -> None:
    code, rows = doctor_rows(ctx, "-H", "codex")
    ctx.eq(code, 0, "doctor -H codex exit")
    ctx.eq(rows.get("agent: codex", {}).get("ok"), True, "agent: codex row")
    ctx.eq(rows.get("login: codex", {}).get("ok"), True, "login: codex row")
    no_bin = ctx.sbx / "stub-claude-only"
    no_bin.mkdir()
    (no_bin / "claude").symlink_to(ctx.fx.stub_bin / "claude")
    (no_bin / "security").symlink_to(ctx.fx.stub_bin / "security")
    path = ctx.env["PATH"].replace(str(ctx.fx.stub_bin), str(no_bin))
    code, rows = doctor_rows(ctx, "-H", "codex", env=dict(ctx.env, PATH=path))
    ctx.eq(code, 1, "doctor -H codex exit without a codex binary")
    ctx.eq(rows.get("agent: codex", {}).get("ok"), False, "agent: codex row fails")
    (ctx.fx.home / ".codex" / "auth.json").unlink()
    code, rows = doctor_rows(ctx, "-H", "codex")
    ctx.eq(code, 1, "doctor -H codex exit without a codex login")
    ctx.eq(rows.get("login: codex", {}).get("ok"), False, "login: codex row fails")
    code, rows = doctor_rows(ctx)
    ctx.eq(code, 0, "doctor (claude) exit: the other harness's login is optional")


def fake_keychain(ctx: Ctx, login: str | None) -> dict[str, str]:
    """An env whose `security` finds ``login`` (None: no item) and logs its
    argv. Boundary: it stands in for the macOS Keychain, never the real one."""
    bin_dir = ctx.sbx / "keychain-bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / "security"
    log = ctx.sbx / "security.log"
    found = f"printf '%s\\n' {shlex.quote(login)}\nexit 0" if login is not None else "exit 44"
    tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(log))}\n{found}\n')
    tool.chmod(0o755)
    return dict(ctx.env, PATH=os.pathsep.join([str(bin_dir), ctx.env["PATH"]]))


@scenario(
    "A10",
    "the macOS Keychain is the claude login when the file is absent",
    requires=("macos",),
    evidence="boundary",
)
def a10(ctx: Ctx) -> None:
    (ctx.fx.home / ".claude" / ".credentials.json").unlink()
    code, rows = doctor_rows(ctx, env=fake_keychain(ctx, '{"keychain": "treebox-e2e"}'))
    ctx.eq(code, 0, "doctor exit with a Keychain login")
    row = rows.get("login: claude", {})
    ctx.eq(row.get("ok"), True, "login: claude row")
    ctx.check("Keychain" in row.get("detail", ""), f"row names the Keychain ({row.get('detail')})")
    log = (ctx.sbx / "security.log").read_text()
    ctx.check("Claude Code-credentials" in log, "treebox asked for the Claude Code item")
    code, rows = doctor_rows(ctx, env=fake_keychain(ctx, None))
    ctx.eq(code, 1, "doctor exit with no file and no Keychain item")
    ctx.eq(rows.get("login: claude", {}).get("ok"), False, "login: claude row fails")


# --- B. create -------------------------------------------------------------------------


@group("B", "create")
def _setup_b(ctx: Ctx) -> None:
    return


@scenario("B1", "named create")
def b1(ctx: Ctx) -> None:
    d = created(ctx, "fix-auth")
    path = wt(ctx, "fix-auth")
    ctx.eq(d.get("name"), "fix-auth", "name")
    ctx.eq(d.get("branch"), "fix-auth", "branch")
    ctx.eq(d.get("base"), "main", "base")
    ctx.eq(d.get("created"), True, "created")
    ctx.eq(d.get("worktree_path"), str(path), "worktree_path")
    entry = d.get("entry_command") or []
    ctx.check(str(path) in " ".join(entry) and "claude" in " ".join(entry), "entry_command")
    ctx.check((path / ".venv").is_dir(), ".venv exists")
    env_file = path / ".env"
    ctx.check(env_file.is_file() and env_file.read_text() == "SECRET=canonical\n", ".env copied")
    ctx.eq(ctx.git("branch", "--show-current", cwd=path), "fix-auth", "checked-out branch")
    ctx.check(bool(re.fullmatch(r"[0-9a-f]{10}", str(d.get("id")))), f"id {d.get('id')!r}")
    ctx.eq(
        (d.get("port_base"), d.get("port_count")), (20000, 10), "first block of the default range"
    )
    ctx.eq(d.get("warnings"), [], "warnings")
    row = listed(ctx).get("fix-auth", {})
    ctx.eq((row.get("id"), row.get("port_base")), (d.get("id"), 20000), "list --json identity")


@scenario("B2", "nameless create gets a petname placeholder")
def b2(ctx: Ctx) -> None:
    d = created(ctx, None)
    name = d.get("name", "")
    ctx.check(bool(re.fullmatch(r"[a-z]+-[a-z]+", name)), f"petname {name!r}")
    ctx.eq(d.get("branch"), f"treebox/{name}", "placeholder branch")
    rows = listed(ctx)
    ctx.eq(rows.get(name, {}).get("unnamed"), True, "list marks it unnamed")


@scenario("B3", "slash name flattens to the directory")
def b3(ctx: Ctx) -> None:
    d = created(ctx, "feature/auth")
    ctx.eq(d.get("branch"), "feature/auth", "branch")
    ctx.eq(d.get("name"), "feature--auth", "name")
    ctx.check(wt(ctx, "feature--auth").is_dir(), "directory feature--auth")
    ctx.git("branch", "treebox/a/b", "origin/main")
    flat = created(ctx, None, "--checkout", "treebox/a/b")
    ctx.eq(flat.get("name"), "a--b", "a placeholder branch drops treebox/, then flattens")
    ctx.check(wt(ctx, "a--b").is_dir() and not wt(ctx, "treebox").exists(), "no nested worktree")


@scenario("B4", "--base cuts from origin/<base>")
def b4(ctx: Ctx) -> None:
    dev = ctx.sbx / "dev-clone"
    ctx.git("worktree", "add", "-q", str(dev), "dev")
    (dev / "DEV.md").write_text("dev only\n")
    ctx.git("add", "DEV.md", cwd=dev)
    ctx.git("commit", "-qm", "dev only", cwd=dev)
    ctx.git("push", "-q", "origin", "dev", cwd=dev)
    ctx.git("worktree", "remove", "--force", str(dev))
    d = created(ctx, "from-dev", "--base", "dev")
    ctx.eq(d.get("base"), "dev", "base")
    head = ctx.git("rev-parse", "HEAD", cwd=wt(ctx, "from-dev"))
    ctx.eq(head, ctx.git("rev-parse", "origin/dev"), "head is origin/dev")
    ctx.check(head != ctx.git("rev-parse", "origin/main"), "origin/dev differs from main")


@scenario("B5", "--checkout an existing branch")
def b5(ctx: Ctx) -> None:
    d = created(ctx, "review-dev", "--checkout", "dev")
    ctx.eq(d.get("branch"), "dev", "branch")
    ctx.eq(ctx.git("branch", "--show-current", cwd=wt(ctx, "review-dev")), "dev", "on dev")
    ctx.check(not branch_exists(ctx, "review-dev"), "no review-dev branch was created")


@scenario("B6", "BRANCH_EXISTS conflict")
def b6(ctx: Ctx) -> None:
    created(ctx, "fix-auth")
    other = ctx.sbx / "wts2"
    run = ctx.tb("create", "fix-auth", *ctx.wt_args(other), "--json")
    ctx.exit_is(run, 5)
    err = ctx.json_err(run, "BRANCH_EXISTS")
    ctx.check("--checkout" in err.get("hint", ""), "hint points at --checkout")
    ctx.check(not (other / "fix-auth").exists(), "nothing created under the other root")


@scenario("B7", "SLUG_CONFLICT on re-create")
def b7(ctx: Ctx) -> None:
    created(ctx, "fix-auth")
    run = ctx.tb("create", "fix-auth", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 5)
    err = ctx.json_err(run, "SLUG_CONFLICT")
    ctx.check("enter" in err.get("hint", ""), "hint points at enter")


@scenario("B8", "invalid and reserved names")
def b8(ctx: Ctx) -> None:
    run = ctx.tb("create", "Bad Name", *ctx.wt_args())
    ctx.exit_is(run, 2)
    ctx.check("slug" in run.stderr.lower() or "lowercase" in run.stderr.lower(), "explains rule")
    reserved = ctx.tb("create", "treebox/mine", *ctx.wt_args(), "--json")
    ctx.exit_is(reserved, 2)
    ctx.json_err(reserved, "INVALID_NAME")
    ctx.check(not wt(ctx, "treebox--mine").exists(), "nothing created")


@scenario("B9", "fetch is required; --no-fetch is the escape")
def b9(ctx: Ctx) -> None:
    ctx.git("remote", "set-url", "origin", "/nonexistent")
    try:
        run = ctx.tb("create", "off-line", *ctx.wt_args(), "--json")
        ctx.exit_is(run, 4)
        ctx.json_err(run, "FETCH_FAILED")
        ctx.check(not wt(ctx, "off-line").exists(), "no worktree after a failed fetch")
        escape = ctx.tb("create", "off-line", *ctx.wt_args(), "--no-fetch", "--print")
        ctx.exit_is(escape, 0)
    finally:
        ctx.git("remote", "set-url", "origin", str(ctx.fx.origin))


@scenario("B10", "--dry-run changes nothing")
def b10(ctx: Ctx) -> None:
    human = ctx.tb("create", "plan-x", *ctx.wt_args(), "--dry-run")
    ctx.exit_is(human, 0)
    ctx.eq(human.stdout, "", "human dry-run stdout")
    ctx.check("git" in human.stderr and "worktree add" in human.stderr, "plan on stderr")
    machine = ctx.tb("create", "plan-x", *ctx.wt_args(), "--dry-run", "--json")
    ctx.exit_is(machine, 0)
    d = ctx.json_out(machine) or {}
    ctx.eq(d.get("dry_run"), True, "dry_run")
    ctx.check(isinstance(d.get("commands"), list) and d["commands"], "commands array")
    ctx.check(not wt(ctx, "plan-x").exists(), "no worktree")


@scenario("B10b", "--dry-run preflights like a real create")
def b10b(ctx: Ctx) -> None:
    created(ctx, "fix-auth")
    before = ctx.git("worktree", "list", "--porcelain")
    other = ctx.sbx / "wts2"
    cases = [
        (["fix-auth", *ctx.wt_args(other)], 5, "BRANCH_EXISTS"),
        (["fix-auth", *ctx.wt_args()], 5, "SLUG_CONFLICT"),
        (["ghost-co", "--checkout", "no-such-branch", *ctx.wt_args()], 3, "NOT_FOUND"),
        (["ghost-base", "--base", "no-such-base", *ctx.wt_args()], 3, "NOT_FOUND"),
        (["dup-co", "--checkout", "fix-auth", *ctx.wt_args()], 5, "BRANCH_IN_USE"),
    ]
    for args, code, error in cases:
        run = ctx.tb("create", *args, "--dry-run", "--json")
        ctx.exit_is(run, code)
        ctx.json_err(run, error)
        ctx.eq(run.stdout, "", f"stdout of dry-run {error}")
    ctx.check(not (other / "fix-auth").exists(), "nothing created under the other root")
    ctx.eq(ctx.git("worktree", "list", "--porcelain"), before, "worktree registrations")


@scenario("B11", "--print emits one self-contained command")
def b11(ctx: Ctx) -> None:
    run = ctx.tb("create", "print-x", *ctx.wt_args(), "--print")
    ctx.exit_is(run, 0)
    ctx.eq(len(run.stdout.splitlines()), 1, "stdout lines")
    ctx.check(str(wt(ctx, "print-x")) in run.stdout, "command carries the worktree dir")
    ctx.eq(host_launch_argv(run)[:1], ["claude"], "command execs claude")


@scenario("B12", "--harness codex")
def b12(ctx: Ctx) -> None:
    run = ctx.tb("create", "cdx", *ctx.wt_args(), "--harness", "codex", "--print")
    ctx.exit_is(run, 0)
    ctx.eq(host_launch_argv(run)[:1], ["codex"], "command execs codex")
    ctx.check("claude" not in run.stdout, "no claude in the command")


@scenario("B13", "pre-push guard blocks treebox/* refs only")
def b13(ctx: Ctx) -> None:
    created(ctx, "fix-auth")
    path = wt(ctx, "fix-auth")
    ctx.git("branch", "treebox/scratch", cwd=path)
    push = ctx.sh("git", "-C", str(path), "push", "origin", "treebox/scratch", check=False)
    ctx.check(push.exit != 0, "push of treebox/scratch is rejected")
    ctx.check("placeholder" in push.stderr, "rejection names the placeholder branch")
    named = ctx.sh("git", "-C", str(path), "push", "-q", "origin", "fix-auth", check=False)
    ctx.eq(named.exit, 0, "push of fix-auth exit")


@scenario("B14", "progress and color degrade off a terminal")
def b14(ctx: Ctx) -> None:
    piped = ctx.tb("create", "b14-pipe", *ctx.wt_args(), "--print")
    ctx.exit_is(piped, 0)
    no_ansi(ctx, piped, "piped create")
    ctx.check(not re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", piped.stderr), "no spinner frames when piped")
    colorless = ctx.tb(
        "create", "b14-nocolor", *ctx.wt_args(), "--print", env=dict(ctx.env, NO_COLOR="1")
    )
    ctx.exit_is(colorless, 0)
    no_ansi(ctx, colorless, "NO_COLOR create")
    quiet = ctx.tb("create", "b14-quiet", *ctx.wt_args(), "--print", "--quiet")
    ctx.exit_is(quiet, 0)
    ctx.eq(quiet.stderr, "", "--quiet stderr")
    verbose = ctx.tb("create", "b14-verbose", *ctx.wt_args(), "--print", "--verbose")
    ctx.exit_is(verbose, 0)
    ctx.check("Creating virtual environment" in verbose.stderr, "--verbose streams uv's output")
    ctx.check("Creating virtual environment" not in piped.stderr, "default mode hides it")


@scenario("B15", "real launch on the host runs the controlled stub")
def b15(ctx: Ctx) -> None:
    run = ctx.tb("create", "launch-host", *ctx.wt_args())
    ctx.exit_is(run, 0)
    rec = stub_record(run.stdout)
    if not ctx.check(rec is not None, "the stub agent ran and printed its record"):
        return
    assert rec is not None
    ctx.eq(rec.get("name"), "claude", "launched harness")
    ctx.eq(rec.get("cwd"), str(wt(ctx, "launch-host")), "agent cwd")
    ctx.eq(rec.get("args"), ["--dangerously-skip-permissions"], "agent argv")
    ctx.eq(rec.get("in_container"), "no", "runs on the host")
    ctx.eq(rec.get("api_key"), "unset", "ANTHROPIC_API_KEY")
    row = listed(ctx).get("launch-host", {})
    ctx.eq(
        (rec.get("treebox_name"), rec.get("treebox_id"), rec.get("treebox_port_base")),
        ("launch-host", row.get("id"), str(row.get("port_base"))),
        "TREEBOX_NAME, TREEBOX_ID, TREEBOX_PORT_BASE in the agent's environment",
    )
    ctx.eq(rec.get("treebox_port_count"), "10", "TREEBOX_PORT_COUNT")


@scenario("B16", "launch without the agent binary fails before provisioning")
def b16(ctx: Ctx) -> None:
    env = without_path_entry(ctx.env, ctx.fx.stub_bin)
    if shutil.which("claude", path=env["PATH"]):
        ctx.skip("a claude binary is reachable without the stub")
    run = ctx.tb("create", "no-agent", *ctx.wt_args(), env=env)
    ctx.exit_is(run, 1)
    ctx.check("'claude' not found on PATH" in run.stderr, "message names the missing binary")
    ctx.check("Traceback" not in run.stderr, "no traceback")
    ctx.check(not wt(ctx, "no-agent").exists(), "nothing provisioned (HARNESS_MISSING first)")
    ctx.check(not branch_exists(ctx, "no-agent"), "no branch")


@scenario("B17", "BRANCH_IN_USE for a checked-out branch")
def b17(ctx: Ctx) -> None:
    created(ctx, "fix-auth")
    run = ctx.tb("create", "dup", "--checkout", "fix-auth", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 5)
    ctx.json_err(run, "BRANCH_IN_USE")
    ctx.check(not wt(ctx, "dup").exists(), "nothing created")


@scenario("B18", "INVALID_BRANCH for a malformed --checkout")
def b18(ctx: Ctx) -> None:
    run = ctx.tb("create", "--checkout", "bad..ref", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 2)
    ctx.json_err(run, "INVALID_BRANCH")


@scenario("B19", "NOT_A_REPO for a --repo outside git")
def b19(ctx: Ctx) -> None:
    plain = ctx.sbx / "not-a-repo"
    plain.mkdir()
    run = ctx.tb("create", "x", "--repo", str(plain), "--json")
    ctx.exit_is(run, 2)
    err = ctx.json_err(run, "NOT_A_REPO")
    ctx.check("--repo" in err.get("hint", ""), "hint names --repo")


@scenario("B20", "GIT_MISSING when git is not on PATH")
def b20(ctx: Ctx) -> None:
    bare = ctx.sbx / "no-git-bin"
    bare.mkdir()
    env = dict(ctx.env, PATH=os.pathsep.join([str(ctx.fx.stub_bin), str(bare)]))
    run = ctx.tb("list", *ctx.wt_args(), "--json", env=env)
    ctx.exit_is(run, 1)
    ctx.json_err(run, "GIT_MISSING")


@scenario("B21", "TEMPLATE_INVALID before anything is provisioned")
def b21(ctx: Ctx) -> None:
    broken = ctx.fx.treebox_home / "templates" / "broken"
    shutil.copytree(OFFLINE_TEMPLATE_DIR, broken, symlinks=False)
    (broken / "container.json").write_text("{ not json")
    args = ["create", "e2e-broken", *ctx.wt_args(), "--isolation", "docker"]
    run = ctx.tb(*args, "--template", "broken", "--json")
    ctx.exit_is(run, 1)
    ctx.json_err(run, "TEMPLATE_INVALID")
    ctx.check(not wt(ctx, "e2e-broken").exists(), "no worktree")
    ctx.check(not branch_exists(ctx, "e2e-broken"), "no branch")


@scenario("B22", "MISSING_DEPENDENCY without a docker CLI")
def b22(ctx: Ctx) -> None:
    tools = ctx.sbx / "toolbox-nodocker"
    tools.mkdir()
    for tool in ("git", "uv"):
        found = shutil.which(tool, path=ctx.env["PATH"])
        if found:
            (tools / tool).symlink_to(found)
    env = dict(ctx.env, PATH=os.pathsep.join([str(ctx.fx.stub_bin), str(tools)]))
    run = ctx.tb(
        "create", "e2e-nodocker", *ctx.wt_args(), "--isolation", "docker", "--json", env=env
    )
    ctx.exit_is(run, 1)
    ctx.json_err(run, "MISSING_DEPENDENCY")
    ctx.check(not wt(ctx, "e2e-nodocker").exists(), "no worktree before preflight passes")


@scenario("B23", "DOCKER_UNAVAILABLE with an unreachable daemon", requires=("docker-cli",))
def b23(ctx: Ctx) -> None:
    env = dict(ctx.env, DOCKER_HOST=f"unix://{ctx.sbx}/no-daemon.sock")
    run = ctx.tb(
        "create", "e2e-nodaemon", *ctx.wt_args(), "--isolation", "docker", "--json", env=env
    )
    ctx.exit_is(run, 1)
    ctx.json_err(run, "DOCKER_UNAVAILABLE")
    ctx.check(not wt(ctx, "e2e-nodaemon").exists(), "no worktree")
    doctor = ctx.tb("doctor", "--repo", str(ctx.repo), "--isolation", "docker", env=env)
    ctx.exit_is(doctor, 1)


@scenario("B24", "--cold builds from source and leaves no temp cache")
def b24(ctx: Ctx) -> None:
    tmp = Path(ctx.env["TMPDIR"])
    before = set(tmp.glob("treebox-cold-*"))
    run = ctx.tb("create", "cold-x", *ctx.wt_args(), "--cold", "--print")
    ctx.exit_is(run, 0)
    ctx.check("from-source (cold)" in run.stderr, "setup reports a cold build")
    ctx.check((wt(ctx, "cold-x") / ".venv").is_dir(), ".venv exists")
    ctx.eq(set(tmp.glob("treebox-cold-*")) - before, set(), "leftover treebox-cold-* dirs")


@scenario("B25", "an unattended launch without a login is LOGIN_MISSING")
def b25(ctx: Ctx) -> None:
    (ctx.fx.home / ".claude" / ".credentials.json").unlink()
    for args, env in (
        (["--non-interactive", "create", "b25"], ctx.env),
        (["create", "b25"], dict(ctx.env, TREEBOX_NON_INTERACTIVE="1")),
    ):
        run = ctx.tb(*args, *ctx.wt_args(), env=env)
        ctx.exit_is(run, 1)
        ctx.check("No claude login for an unattended launch" in run.stderr, "says why")
        ctx.check(stub_record(run.stdout) is None, "the agent did not start")
    ctx.check(not wt(ctx, "b25").exists(), "nothing provisioned")
    ctx.check(not branch_exists(ctx, "b25"), "no branch")


@scenario("B26", "resource limit flags: unsupported on host, invalid values refused")
def b26(ctx: Ctx) -> None:
    host = ctx.tb("create", "b26", *ctx.wt_args(), "--memory", "4g", "--json")
    ctx.exit_is(host, 2)
    ctx.json_err(host, "RESOURCE_UNSUPPORTED")
    bad = ctx.tb(
        "create", "e2e-b26", *ctx.wt_args(), "--isolation", "docker", "--memory", "lots", "--json"
    )
    ctx.exit_is(bad, 2)
    ctx.json_err(bad, "INVALID_RESOURCE")
    for name in ("b26", "e2e-b26"):
        ctx.check(not wt(ctx, name).exists() and not branch_exists(ctx, name), f"no {name}")
    plan = ctx.tb(
        "create",
        "e2e-b26",
        *ctx.wt_args(),
        "--isolation",
        "docker",
        "--cpus",
        "1.5",
        "--memory",
        "512m",
        "--pids-limit",
        "64",
        "--dry-run",
        "--json",
    )
    ctx.exit_is(plan, 0)
    d = ctx.json_out(plan) or {}
    ctx.eq(d.get("resources"), {"cpus": 1.5, "memory": "512m", "pids": 64}, "planned resources")
    run_cmd = next((c for c in d.get("commands", []) if c.startswith("docker run ")), "")
    ctx.check(
        "--cpus 1.5 --memory 512m --pids-limit 64" in run_cmd, "docker run carries the limits"
    )


@scenario("B27", "INVALID_ROOT for a root that is not a directory")
def b27(ctx: Ctx) -> None:
    created(ctx, "b27")
    afile = ctx.sbx / "afile"
    afile.write_text("not a directory\n")
    for root in (afile, afile / "below"):
        for args in (("create", "b27x"), ("enter", "b27"), ("list",), ("teardown", "b27")):
            run = ctx.tb(*args, *ctx.wt_args(root), "--json")
            ctx.exit_is(run, 2)
            ctx.json_err(run, "INVALID_ROOT")
    ctx.check(wt(ctx, "b27").is_dir(), "the real worktree untouched")


@scenario("B28", "the branch's own .gitmodules decides the submodule copy and the plan")
def b28(ctx: Ctx) -> None:
    add_submodule(ctx)
    plan = ctx.tb("create", "b28", *ctx.wt_args(), "--dry-run", "--json")
    commands = (ctx.json_out(plan) or {}).get("commands", [])
    ctx.check(any(c.startswith("# copy submodule sub ") for c in commands), "plan copies sub")
    gitmodules = ctx.git("show", "main:.gitmodules")
    ghost = '[submodule "ghost"]\n\tpath = ghost\n\turl = ../ghost.git\n'
    commit_with_index(ctx, "b28-extra", [("100644", ".gitmodules", gitmodules + ghost)])
    commit_with_index(ctx, "b28-nosub", [("remove", ".gitmodules", ""), ("remove", "sub", "")])
    extra = ctx.tb("create", "--checkout", "b28-extra", *ctx.wt_args(), "--dry-run", "--json")
    lines = (ctx.json_out(extra) or {}).get("commands", [])
    ctx.check(any("submodules: skipped ghost" in c for c in lines), "plan names the skipped path")
    run = ctx.tb("create", "--checkout", "b28-extra", *ctx.wt_args(), "--print")
    ctx.exit_is(run, 0)
    ctx.check("skipped ghost" in run.stderr, "the run notes the skipped path")
    path = wt(ctx, "b28-extra")
    ctx.eq((path / "sub" / "lib.txt").read_text(), "hello from submodule\n", "sub copied")
    ctx.eq(ctx.git("status", "--porcelain", cwd=path), "", "fresh worktree is clean")
    nosub = ctx.tb("create", "--checkout", "b28-nosub", *ctx.wt_args(), "--json")
    ctx.exit_is(nosub, 0)
    path = wt(ctx, "b28-nosub")
    ctx.check(not (path / "sub").exists(), "a branch without the submodule gets no copy")
    ctx.eq(ctx.git("status", "--porcelain", cwd=path), "", "and stays clean")


@scenario("B29", "docker isolation as root is RUNNING_AS_ROOT", requires=("root", "docker-cli"))
def b29(ctx: Ctx) -> None:
    run = ctx.tb("create", "e2e-root", *ctx.wt_args(), "--isolation", "docker", "--json")
    ctx.exit_is(run, 1)
    ctx.json_err(run, "RUNNING_AS_ROOT")
    ctx.check(not wt(ctx, "e2e-root").exists(), "nothing provisioned")


# --- C. enter ----------------------------------------------------------------------------


@group("C", "enter")
def _setup_c(ctx: Ctx) -> None:
    created(ctx, "fix-auth")


@scenario("C1", "re-launch --print")
def c1(ctx: Ctx) -> None:
    run = ctx.tb("enter", "fix-auth", *ctx.wt_args(), "--print")
    ctx.exit_is(run, 0)
    ctx.eq(len(run.stdout.splitlines()), 1, "stdout lines")
    ctx.check(str(wt(ctx, "fix-auth")) in run.stdout, "carries the worktree dir")
    ctx.eq(host_launch_argv(run)[:1], ["claude"], "execs claude")


@scenario("C2", "enter refreshes .env")
def c2(ctx: Ctx) -> None:
    (ctx.repo / ".env").write_text("SECRET=rotated\n")
    ctx.exit_is(ctx.tb("enter", "fix-auth", *ctx.wt_args(), "--print"), 0)
    ctx.eq((wt(ctx, "fix-auth") / ".env").read_text(), "SECRET=rotated\n", "worktree .env")


@scenario("C3", "deps re-sync only when the manifest changed")
def c3(ctx: Ctx) -> None:
    path = wt(ctx, "fix-auth")
    pyproject = path / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('"0.1.0"', '"0.1.1"'))
    ctx.sh("uv", "lock", "-q", cwd=path)
    ctx.eq(listed(ctx).get("fix-auth", {}).get("deps"), "stale", "deps after manifest change")
    ctx.exit_is(ctx.tb("enter", "fix-auth", *ctx.wt_args(), "--print"), 0)
    ctx.eq(listed(ctx).get("fix-auth", {}).get("deps"), "fresh", "deps after enter")


@scenario("C4", "ref resolution survives a branch rename")
def c4(ctx: Ctx) -> None:
    ctx.git("branch", "-m", "fix/auth-renamed", cwd=wt(ctx, "fix-auth"))
    for ref in ("fix-auth", "fix/auth-renamed", "renamed"):
        run = ctx.tb("enter", ref, *ctx.wt_args(), "--json")
        ctx.exit_is(run, 0)
        d = ctx.json_out(run) or {}
        ctx.eq(d.get("worktree_path"), str(wt(ctx, "fix-auth")), f"path for ref {ref!r}")


@scenario("C5", "ambiguous and missing refs")
def c5(ctx: Ctx) -> None:
    created(ctx, "amb-one")
    created(ctx, "amb-two")
    run = ctx.tb("enter", "amb", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 2)
    err = ctx.json_err(run, "AMBIGUOUS_REF")
    text = json.dumps(err)
    ctx.check("amb-one" in text and "amb-two" in text, "error lists both candidates")
    ghost = ctx.tb("enter", "ghost-x", *ctx.wt_args(), "--json")
    ctx.exit_is(ghost, 3)
    ctx.json_err(ghost, "NOT_FOUND")


@scenario("C6", "agent args pass through verbatim")
def c6(ctx: Ctx) -> None:
    run = ctx.tb("enter", "fix-auth", *ctx.wt_args(), "--print", "--", "continue the refactor")
    ctx.exit_is(run, 0)
    ctx.eq(
        host_launch_argv(run),
        ["claude", "--dangerously-skip-permissions", "continue the refactor"],
        "agent argv",
    )


@scenario("C7", "recorded isolation is sticky")
def c7(ctx: Ctx) -> None:
    run = ctx.tb("enter", "fix-auth", *ctx.wt_args(), "--isolation", "docker", "--json")
    ctx.exit_is(run, 5)
    ctx.json_err(run, "ISOLATION_MISMATCH")


@scenario("C8", "real launch on enter propagates the agent's exit status")
def c8(ctx: Ctx) -> None:
    run = ctx.tb(
        "enter",
        "fix-auth",
        *ctx.wt_args(),
        "--",
        "--stub-exit=7",
        "continue the refactor",
        tags=("agent-exit",),
    )
    ctx.exit_is(run, 7)
    ctx.eq(
        run.stderr.splitlines()[-1:], ["treebox: agent exited with status 7"], "last stderr line"
    )
    rec = stub_record(run.stdout) or {}
    ctx.eq(
        rec.get("args"),
        ["--dangerously-skip-permissions", "--stub-exit=7", "continue the refactor"],
        "agent argv",
    )
    ctx.eq(rec.get("cwd"), str(wt(ctx, "fix-auth")), "agent cwd")


def record_file(ctx: Ctx, name: str, root: Path | None = None) -> Path:
    """The host-owned record of a worktree, beside the worktree root."""
    return (root or ctx.root) / ".records" / f"{name}.json"


@scenario("C9", "RECORD_INVALID: an unknown recorded isolation is refused, teardown recovers")
def c9(ctx: Ctx) -> None:
    created(ctx, "c9-corrupt")
    rec = record_file(ctx, "c9-corrupt")
    data = json.loads(rec.read_text())
    data["isolation"] = "qemu"
    rec.write_text(json.dumps(data))
    run = ctx.tb("enter", "c9-corrupt", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 5)
    err = ctx.json_err(run, "RECORD_INVALID")
    ctx.eq(err.get("path"), str(rec), "error.path is the record")
    row = listed(ctx).get("c9-corrupt", {})
    ctx.eq((row.get("health"), row.get("isolation")), ("unreadable_state", ""), "list row")
    td = ctx.tb("teardown", "c9-corrupt", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(td, 0)
    ctx.check(not wt(ctx, "c9-corrupt").exists() and not rec.exists(), "worktree and record gone")


@scenario("C10", "recorded harness wins; --harness overrides one session")
def c10(ctx: Ctx) -> None:
    created(ctx, "c10-codex", "--harness", "codex")
    run = ctx.tb("enter", "c10-codex", *ctx.wt_args(), "--print")
    ctx.eq(host_launch_argv(run)[:1], ["codex"], "recorded harness")
    override = ctx.tb("enter", "c10-codex", *ctx.wt_args(), "--harness", "claude", "--print")
    ctx.eq(host_launch_argv(override)[:1], ["claude"], "explicit --harness")
    again = ctx.tb("enter", "c10-codex", *ctx.wt_args(), "--print")
    ctx.eq(host_launch_argv(again)[:1], ["codex"], "the override did not stick")


@scenario("C11", "enter --cold re-syncs from source")
def c11(ctx: Ctx) -> None:
    created(ctx, "c11-cold")
    path = wt(ctx, "c11-cold")
    pyproject = path / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('"0.1.0"', '"0.2.0"'))
    ctx.sh("uv", "lock", "-q", cwd=path)
    run = ctx.tb("enter", "c11-cold", *ctx.wt_args(), "--cold", "--print")
    ctx.exit_is(run, 0)
    ctx.check("from-source (cold)" in run.stderr, "re-sync reports a cold build")
    ctx.eq(listed(ctx).get("c11-cold", {}).get("deps"), "fresh", "deps after enter --cold")


@scenario("C12", "enter --quiet and --verbose")
def c12(ctx: Ctx) -> None:
    quiet = ctx.tb("enter", "fix-auth", *ctx.wt_args(), "--quiet", "--print")
    ctx.exit_is(quiet, 0)
    ctx.eq(quiet.stderr, "", "--quiet stderr")
    path = wt(ctx, "fix-auth")
    pyproject = path / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('version = "', 'version = "1'))
    ctx.sh("uv", "lock", "-q", cwd=path)
    verbose = ctx.tb("enter", "fix-auth", *ctx.wt_args(), "--verbose", "--print")
    ctx.exit_is(verbose, 0)
    ctx.check("Resolved" in verbose.stderr, "--verbose streams uv's output on re-sync")


@scenario("C13", "policy comes from the host-owned record, never the sandbox-writable git dir")
def c13(ctx: Ctx) -> None:
    path = wt(ctx, "fix-auth")
    gitdir = Path(ctx.git("rev-parse", "--absolute-git-dir", cwd=path))
    planted = {"isolation": "docker", "template": "/etc", "provisioned": True}
    (gitdir / "treebox-state.json").write_text(json.dumps(planted))
    run = ctx.tb("enter", "fix-auth", *ctx.wt_args(), "--print")
    ctx.exit_is(run, 0)
    ctx.eq(host_launch_argv(run)[:1], ["claude"], "still the host launch command")
    ctx.eq(listed(ctx).get("fix-auth", {}).get("isolation"), "host", "list isolation")
    ctx.eq(json.loads(record_file(ctx, "fix-auth").read_text()).get("isolation"), "host", "record")


@scenario("C14", "a failed setup is a failed create; enter retries it")
def c14(ctx: Ctx) -> None:
    ctx.write_config('setup_hook = ["exit 7"]\n')
    run = ctx.tb("create", "c14", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 1)
    err = ctx.json_err(run, "SETUP_FAILED")
    message = err.get("message", "")
    ctx.check("setup · hook 1" in message and "status 7" in message, f"names the step: {message}")
    log = ctx.root / ".logs" / "c14.setup.log"
    ctx.eq(err.get("path"), str(log), "error.path")
    ctx.check(log.is_file(), "the step's output is kept outside the worktree")
    ctx.check(wt(ctx, "c14").is_dir(), "the worktree is kept")
    row = listed(ctx).get("c14", {})
    ctx.eq((row.get("setup"), row.get("deps")), ("failed", "unknown"), "list setup, deps")
    again = ctx.tb("enter", "c14", *ctx.wt_args(), "--json")
    ctx.exit_is(again, 1)
    ctx.json_err(again, "SETUP_FAILED")
    ctx.write_config('setup_hook = ["true"]\n')
    fixed = ctx.tb("enter", "c14", *ctx.wt_args(), "--json")
    ctx.exit_is(fixed, 0)
    row = listed(ctx).get("c14", {})
    ctx.eq((row.get("setup"), row.get("deps")), ("done", "fresh"), "after the retry")
    ctx.write_config("")


@scenario("C15", "INVALID_LINKAGE: a missing .git pointer is refused; teardown recovers")
def c15(ctx: Ctx) -> None:
    created(ctx, "c15")
    path = wt(ctx, "c15")
    (path / ".git").unlink()
    run = ctx.tb("enter", "c15", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 5)
    ctx.json_err(run, "INVALID_LINKAGE")
    row = listed(ctx).get("c15", {})
    ctx.eq((row.get("health"), row.get("missing")), ("invalid_linkage", True), "list row")
    ctx.exit_is(ctx.tb("teardown", "c15", *ctx.wt_args(), "--force", "--json"), 0)
    ctx.check(
        not path.exists() and not worktree_registered(ctx, path), "directory and registration"
    )


@scenario("C16", "RECORD_MISSING: no record means recreate; teardown needs no extra flag")
def c16(ctx: Ctx) -> None:
    run = ctx.tb("create", "e2e-norec", *ctx.wt_args(), "--print")
    ctx.exit_is(run, 0)
    record_file(ctx, "e2e-norec").unlink()
    enter = ctx.tb("enter", "e2e-norec", *ctx.wt_args(), "--json")
    ctx.exit_is(enter, 5)
    hint = ctx.json_err(enter, "RECORD_MISSING").get("hint", "")
    ctx.check("treebox teardown e2e-norec" in hint and "treebox create" in hint, f"hint: {hint}")
    ctx.eq(enter.stdout, "", "nothing emitted")
    row = listed(ctx).get("e2e-norec", {})
    ctx.eq((row.get("isolation"), row.get("health")), ("", "unreadable_state"), "list row")
    td = ctx.tb("teardown", "e2e-norec", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(td, 0)
    ctx.check(not wt(ctx, "e2e-norec").exists(), "worktree gone")


@scenario("C17", "RECORD_CONFLICT: a resumed create must ask for the recorded settings")
def c17(ctx: Ctx) -> None:
    ctx.write_config('setup_hook = ["exit 7"]\n')
    first = ctx.tb("create", "c17", *ctx.wt_args(), "--harness", "codex", "--json")
    ctx.exit_is(first, 1)
    ctx.json_err(first, "SETUP_FAILED")
    ctx.write_config("")
    other = ctx.tb("create", "c17", *ctx.wt_args(), "--harness", "claude", "--json")
    ctx.exit_is(other, 5)
    hint = ctx.json_err(other, "RECORD_CONFLICT").get("hint", "")
    ctx.check("-H codex" in hint, f"hint gives the recorded settings: {hint}")
    same = ctx.tb("create", "c17", *ctx.wt_args(), "--harness", "codex", "--json")
    ctx.exit_is(same, 0)
    ctx.eq(listed(ctx).get("c17", {}).get("setup"), "done", "the resumed create finished setup")


@scenario("C18", "an absolute path is an exact ref for enter and teardown")
def c18(ctx: Ctx) -> None:
    path = wt(ctx, "fix-auth")
    run = ctx.tb("enter", str(path), *ctx.wt_args(), "--json")
    ctx.exit_is(run, 0)
    ctx.eq((ctx.json_out(run) or {}).get("worktree_path"), str(path), "worktree_path")
    near = ctx.tb("enter", str(ctx.root / "fix"), *ctx.wt_args(), "--json")
    ctx.exit_is(near, 3)
    ctx.json_err(near, "NOT_FOUND")
    near_td = ctx.tb("teardown", str(ctx.root / "fix"), *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(near_td, 3)
    ctx.check(path.is_dir(), "a path never falls back to a substring match")
    td = ctx.tb("teardown", str(path), *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(td, 0)
    ctx.check(not path.exists(), "removed by its path")


# --- D. list -------------------------------------------------------------------------------


@group("D", "list")
def _setup_d(ctx: Ctx) -> None:
    created(ctx, "d-one")
    created(ctx, "d-two")
    ctx.git("branch", "-m", "feature/renamed", cwd=wt(ctx, "d-two"))
    # A registration treebox never provisioned: no recorded state.
    ctx.git("worktree", "add", "-q", "-b", "d-raw", str(wt(ctx, "d-raw")), "main")


@scenario("D1", "human table")
def d1(ctx: Ctx) -> None:
    run = ctx.tb("list", *ctx.wt_args())
    ctx.exit_is(run, 0)
    rows = {line.split()[0]: line for line in run.stdout.splitlines() if line.strip()}
    for name in ("d-one", "d-two", "d-raw"):
        ctx.check(name in rows, f"row for {name}")
    ctx.check("feature/renamed" in rows.get("d-two", ""), "renamed branch under the stable name")
    ctx.check("unknown" in rows.get("d-raw", ""), "stateless registration shows unknown")
    ctx.check("host" not in rows.get("d-raw", ""), "no config default for unknown isolation")
    # Piped output wraps at 80 columns, so a long root may span two lines.
    flat = re.sub(r"\s+", "", run.stdout)
    ctx.check(f"Root:{ctx.root}" in flat, "worktree-root footer")


@scenario("D2", "json shape")
def d2(ctx: Ctx) -> None:
    rows = listed(ctx)
    keys = {"name", "branch", "unnamed", "missing", "path", "base", "isolation", "harness"}
    keys |= {"deps", "env", "health", "setup", "id", "port_base", "port_count", "resources"}
    for name in ("d-one", "d-two", "d-raw"):
        row = rows.get(name, {})
        ctx.eq(sorted(keys - set(row)), [], f"missing keys in {name}")
    one = rows.get("d-one", {})
    ctx.eq((one.get("deps"), one.get("env")), ("fresh", "present"), "d-one deps, env")
    ctx.eq((one.get("health"), one.get("setup")), ("healthy", "done"), "d-one health, setup")
    raw = rows.get("d-raw", {})
    ctx.eq(raw.get("isolation"), "", "d-raw isolation (no record)")
    ctx.eq((raw.get("health"), raw.get("setup")), ("unreadable_state", ""), "d-raw health, setup")
    ctx.eq((raw.get("port_base"), raw.get("resources")), (None, None), "d-raw block, limits")


@scenario("D3", "empty root lists nothing")
def d3(ctx: Ctx) -> None:
    run = ctx.tb("list", *ctx.wt_args(ctx.sbx / "empty"), "--json")
    ctx.exit_is(run, 0)
    ctx.eq((ctx.json_out(run) or {}).get("worktrees"), [], "worktrees")


@scenario("D4", "ls is list")
def d4(ctx: Ctx) -> None:
    a = ctx.tb("list", *ctx.wt_args(), "--json")
    b = ctx.tb("ls", *ctx.wt_args(), "--json")
    ctx.exit_is(b, 0)
    strip = re.compile(r'"(commit_epoch|last_commit)": [^,\n]*')
    ctx.eq(strip.sub("", b.stdout), strip.sub("", a.stdout), "ls --json output")


# --- E. teardown -----------------------------------------------------------------------------


@group("E", "teardown")
def _setup_e(ctx: Ctx) -> None:
    created(ctx, "e2e-clean-td")
    created(ctx, "fix-auth")
    ctx.git("branch", "-m", "fix/auth-renamed", cwd=wt(ctx, "fix-auth"))


@scenario("E1", "non-TTY teardown needs --yes; --yes confirms only")
def e1(ctx: Ctx) -> None:
    path = wt(ctx, "e2e-clean-td")
    ctx.eq(ctx.git("status", "--porcelain", cwd=path), "", "precondition: clean worktree")
    run = ctx.tb("teardown", "e2e-clean-td", *ctx.wt_args())
    ctx.exit_is(run, 5)
    ctx.check("--yes" in run.stderr, "message names --yes")
    machine = ctx.tb("teardown", "e2e-clean-td", *ctx.wt_args(), "--json")
    ctx.exit_is(machine, 5)
    ctx.json_err(machine, "NEEDS_CONFIRMATION")
    ctx.check(path.is_dir(), "worktree untouched")
    (wt(ctx, "fix-auth") / "junk.txt").write_text("junk\n")
    dirty = ctx.tb("teardown", "fix-auth", *ctx.wt_args(), "--yes", "--json")
    ctx.exit_is(dirty, 5)
    ctx.json_err(dirty, "DIRTY_WORKTREE")
    ctx.check(wt(ctx, "fix-auth").is_dir(), "--yes keeps the uncommitted-changes check")
    yes = ctx.tb("teardown", "e2e-clean-td", *ctx.wt_args(), "-y", "--json")
    ctx.exit_is(yes, 0)
    ctx.check(not path.exists(), "removed with --yes")


@scenario("E2", "dirty worktree needs --force")
def e2(ctx: Ctx) -> None:
    path = wt(ctx, "fix-auth")
    (path / "junk.txt").write_text("junk\n")
    run = ctx.tb("teardown", "fix-auth", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 5)
    ctx.json_err(run, "DIRTY_WORKTREE")
    ctx.check(path.is_dir(), "worktree intact")
    forced = ctx.tb("teardown", "fix-auth", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(forced, 0)
    ctx.check(not path.exists(), "worktree removed")


@scenario("E3", "forced removal keeps the branch")
def e3(ctx: Ctx) -> None:
    ctx.exit_is(ctx.tb("teardown", "fix-auth", *ctx.wt_args(), "--force", "--json"), 0)
    path = wt(ctx, "fix-auth")
    ctx.check(not path.exists(), "directory gone")
    ctx.check(not worktree_registered(ctx, path), "registration gone")
    ctx.check(branch_exists(ctx, "fix/auth-renamed"), "branch kept")


@scenario("E4", "--delete-branch")
def e4(ctx: Ctx) -> None:
    created(ctx, "del-me")
    run = ctx.tb("teardown", "del-me", *ctx.wt_args(), "--force", "--delete-branch", "--json")
    ctx.exit_is(run, 0)
    records = (ctx.json_out(run) or {}).get("worktrees", [])
    ctx.eq([(r["name"], r["branch_deleted"]) for r in records], [("del-me", True)], "record")
    ctx.check(not branch_exists(ctx, "del-me"), "branch gone")


@scenario("E5", "several refs in one call")
def e5(ctx: Ctx) -> None:
    created(ctx, "multi-a")
    created(ctx, "multi-b")
    run = ctx.tb("teardown", "multi-a", "multi-b", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(run, 0)
    names = sorted(r["name"] for r in (ctx.json_out(run) or {}).get("worktrees", []))
    ctx.eq(names, ["multi-a", "multi-b"], "records")
    ctx.check(not wt(ctx, "multi-a").exists() and not wt(ctx, "multi-b").exists(), "both gone")


@scenario("E6", "the original name resolves after a rename")
def e6(ctx: Ctx) -> None:
    created(ctx, "e6-name")
    ctx.git("branch", "-m", "e6/renamed", cwd=wt(ctx, "e6-name"))
    run = ctx.tb("teardown", "e6-name", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(run, 0)
    records = (ctx.json_out(run) or {}).get("worktrees", [])
    ctx.eq([r.get("branch") for r in records], ["e6/renamed"], "record branch")
    ctx.check(not wt(ctx, "e6-name").exists(), "removed")


@scenario("E7", "a corrupt worktree is not dirty")
def e7(ctx: Ctx) -> None:
    created(ctx, "e2e-corrupt")
    path = wt(ctx, "e2e-corrupt")
    (path / ".git").unlink()
    dirt = ctx.repo / "main-dirt.txt"
    dirt.write_text("main checkout dirt\n")
    try:
        run = ctx.tb("teardown", "e2e-corrupt", *ctx.wt_args(), "--json")
        ctx.exit_is(run, 5)
        ctx.json_err(run, "NEEDS_CONFIRMATION")
        forced = ctx.tb("teardown", "e2e-corrupt", *ctx.wt_args(), "--force", "--json")
        ctx.exit_is(forced, 0)
        ctx.check(not path.exists(), "directory gone")
        ctx.check(not worktree_registered(ctx, path), "registration pruned")
        ctx.check(dirt.is_file(), "main checkout dirt untouched")
    finally:
        dirt.unlink(missing_ok=True)


@scenario("E8", "rm is teardown")
def e8(ctx: Ctx) -> None:
    created(ctx, "rm-me")
    run = ctx.tb("rm", "rm-me", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(run, 0)
    ctx.check(not wt(ctx, "rm-me").exists(), "removed")


@scenario("E9", "MAIN_WORKTREE is never removed")
def e9(ctx: Ctx) -> None:
    ctx.git("branch", ctx.repo.name)
    run = ctx.tb(
        "teardown",
        ctx.repo.name,
        "--repo",
        str(ctx.repo),
        "--root",
        str(ctx.sbx),
        "--force",
        "--json",
    )
    ctx.exit_is(run, 5)
    ctx.json_err(run, "MAIN_WORKTREE")
    ctx.check((ctx.repo / "pyproject.toml").is_file(), "main checkout intact")


@scenario("E10", "a git-locked worktree is never removed, also with --force")
def e10(ctx: Ctx) -> None:
    path = wt(ctx, "e2e-clean-td")
    ctx.git("worktree", "lock", str(path))
    run = ctx.tb("teardown", "e2e-clean-td", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(run, 5)
    hint = ctx.json_err(run, "WORKTREE_LOCKED").get("hint", "")
    ctx.check("worktree unlock" in hint, "hint says how to unlock")
    ctx.check(path.is_dir() and worktree_registered(ctx, path), "nothing removed")
    ctx.git("worktree", "unlock", str(path))
    ctx.exit_is(ctx.tb("teardown", "e2e-clean-td", *ctx.wt_args(), "--force", "--json"), 0)


@scenario("E11", "teardown --quiet and --verbose")
def e11(ctx: Ctx) -> None:
    created(ctx, "e11-q")
    created(ctx, "e11-v")
    quiet = ctx.tb("teardown", "e11-q", *ctx.wt_args(), "--force", "--quiet")
    ctx.exit_is(quiet, 0)
    ctx.eq(quiet.stderr, "", "--quiet stderr")
    verbose = ctx.tb("teardown", "e11-v", *ctx.wt_args(), "--force", "--verbose")
    ctx.exit_is(verbose, 0)
    ctx.check("removed" in verbose.stderr, "--verbose reports the removal")


# --- F. template ------------------------------------------------------------------------------


@group("F", "template")
def _setup_f(ctx: Ctx) -> None:
    return


def templates(ctx: Ctx) -> dict[str, dict[str, Any]]:
    run = ctx.tb("template", "list", "--json")
    ctx.exit_is(run, 0)
    return {t["name"]: t for t in (ctx.json_out(run) or {}).get("templates", [])}


@scenario("F1", "list shows the bundled default")
def f1(ctx: Ctx) -> None:
    human = ctx.tb("template", "list")
    ctx.exit_is(human, 0)
    row = templates(ctx).get("default", {})
    ctx.eq(row.get("source"), "bundled", "default source")
    ctx.eq(row.get("default"), True, "default flag")
    ctx.eq(row.get("valid"), True, "default valid")


@scenario("F2", "path resolution")
def f2(ctx: Ctx) -> None:
    for args in (("template", "path"), ("template", "path", "default")):
        run = ctx.tb(*args)
        ctx.exit_is(run, 0)
        ctx.check(Path(run.stdout.strip()).is_dir(), f"{' '.join(args)} prints a directory")
    ghost = ctx.tb("template", "path", "ghost")
    ctx.exit_is(ghost, 3)
    for name in ("/etc", "../x"):
        ctx.exit_is(ctx.tb("template", "path", name), 2)


@scenario("F3", "init scaffolds the full file set under TREEBOX_HOME")
def f3(ctx: Ctx) -> None:
    run = ctx.tb("template", "init", "mytpl", "--json")
    ctx.exit_is(run, 0)
    t = (ctx.json_out(run) or {}).get("template", {})
    dest = ctx.fx.treebox_home / "templates" / "mytpl"
    ctx.eq(t.get("path"), str(dest), "path under $TREEBOX_HOME")
    ctx.eq(t.get("valid"), True, "valid")
    ctx.eq(t.get("missing"), [], "missing")
    for f in ("Dockerfile", "container.json", "post-create.sh", "init-firewall.sh"):
        ctx.check((dest / f).is_file(), f"{f} scaffolded")
    ctx.check("mytpl" in templates(ctx), "listed")


@scenario("F4", "init conflict and --force")
def f4(ctx: Ctx) -> None:
    ctx.exit_is(ctx.tb("template", "init", "mytpl", "--json"), 0)
    dest = ctx.fx.treebox_home / "templates" / "mytpl"
    marker = dest / "MARKER"
    marker.write_text("keep\n")
    run = ctx.tb("template", "init", "mytpl", "--json")
    ctx.exit_is(run, 5)
    ctx.json_err(run, "TEMPLATE_EXISTS")
    ctx.check(marker.is_file(), "existing template untouched")
    forced = ctx.tb("template", "init", "mytpl", "--force")
    ctx.exit_is(forced, 0)
    ctx.check(not marker.exists() and (dest / "Dockerfile").is_file(), "re-scaffolded")


@scenario("F5", "init --from copies another template")
def f5(ctx: Ctx) -> None:
    ctx.exit_is(ctx.tb("template", "init", "mytpl", "--json"), 0)
    src = ctx.fx.treebox_home / "templates" / "mytpl" / "Dockerfile"
    src.write_text(src.read_text() + "\n# from-mytpl-marker\n")
    run = ctx.tb("template", "init", "copytpl", "--from", "mytpl", "--json")
    ctx.exit_is(run, 0)
    copied = ctx.fx.treebox_home / "templates" / "copytpl" / "Dockerfile"
    ctx.check("from-mytpl-marker" in copied.read_text(), "copied from mytpl")


@scenario("F6", "init --from an unknown template")
def f6(ctx: Ctx) -> None:
    run = ctx.tb("template", "init", "x", "--from", "ghost", "--json")
    ctx.exit_is(run, 3)
    ctx.json_err(run, "TEMPLATE_NOT_FOUND")
    ctx.check(not (ctx.fx.treebox_home / "templates" / "x").exists(), "nothing scaffolded")


@scenario("F7", "template ls is template list")
def f7(ctx: Ctx) -> None:
    a = ctx.tb("template", "list", "--json")
    b = ctx.tb("template", "ls", "--json")
    ctx.exit_is(b, 0)
    ctx.eq(b.stdout, a.stdout, "template ls --json output")


@scenario("F8", "init refuses bad names and a self-copy")
def f8(ctx: Ctx) -> None:
    ctx.exit_is(ctx.tb("template", "init", "mytpl", "--json"), 0)
    bad = ctx.tb("template", "init", "../escape", "--json")
    ctx.exit_is(bad, 2)
    ctx.json_err(bad, "INVALID_NAME")
    same = ctx.tb("template", "init", "mytpl", "--from", "mytpl", "--force", "--json")
    ctx.exit_is(same, 2)
    ctx.json_err(same, "TEMPLATE_CONFLICT")
    ctx.check((ctx.fx.treebox_home / "templates" / "mytpl" / "Dockerfile").is_file(), "intact")


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@scenario("F9", "init --force is all-or-nothing", requires=("non-root",))
def f9(ctx: Ctx) -> None:
    tpl_root = ctx.fx.treebox_home / "templates"
    tpl_root.mkdir(parents=True)
    tpl_root.chmod(0o555)
    try:
        run = ctx.tb("template", "init", "denied", "--json")
        ctx.exit_is(run, 1)
        ctx.json_err(run, "COPY_FAILED")
    finally:
        tpl_root.chmod(0o755)
    ctx.exit_is(ctx.tb("template", "init", "copytpl", "--json"), 0)
    old = tpl_root / "copytpl"
    (old / "MARKER").write_text("old template\n")
    before = tree_bytes(old)
    # The staging dir exists (the init above made it), but the old template
    # cannot be moved aside: the overwrite must leave it byte-for-byte.
    tpl_root.chmod(0o555)
    try:
        over = ctx.tb("template", "init", "copytpl", "--force", "--json")
        ctx.exit_is(over, 1)
        ctx.json_err(over, "OVERWRITE_FAILED")
    finally:
        tpl_root.chmod(0o755)
    ctx.eq(tree_bytes(old) == before, True, "old template unchanged after OVERWRITE_FAILED")
    # A read-only dir in the old template: the overwrite succeeds with the
    # complete new template, and the leftover old copy is a warning.
    locked = old / "locked"
    locked.mkdir()
    (locked / "file").write_text("x\n")
    locked.chmod(0o555)
    try:
        forced = ctx.tb("template", "init", "copytpl", "--force")
        ctx.exit_is(forced, 0)
        ctx.check("could not remove the previous template copy" in forced.stderr, "warns")
    finally:
        for p in (tpl_root / ".treebox-tmp").rglob("locked"):
            p.chmod(0o755)
    ctx.check(not (old / "MARKER").exists(), "the new template replaced the old one")
    ctx.eq(templates(ctx).get("copytpl", {}).get("valid"), True, "the new template is complete")


# --- G. config ------------------------------------------------------------------------------------


@group("G", "config and environment variables")
def _setup_g(ctx: Ctx) -> None:
    return


def with_config(ctx: Ctx, text: str) -> None:
    ctx.write_config(text)


@scenario("G1", "a missing TREEBOX_CONFIG is loud")
def g1(ctx: Ctx) -> None:
    env = dict(ctx.env, TREEBOX_CONFIG=str(ctx.sbx / "nope.toml"))
    run = ctx.tb("list", *ctx.wt_args(), "--json", env=env)
    ctx.exit_is(run, 2)
    err = ctx.json_err(run, "INVALID_CONFIG")
    ctx.check("unset" in err.get("hint", ""), "hint says fix or unset it")


@scenario("G2", "config base applies")
def g2(ctx: Ctx) -> None:
    with_config(ctx, 'base = "dev"\n')
    try:
        ctx.eq(created(ctx, "cfg-base").get("base"), "dev", "base")
    finally:
        with_config(ctx, "")


@scenario("G3", "invalid isolation in config")
def g3(ctx: Ctx) -> None:
    with_config(ctx, 'isolation = "qemu"\n')
    try:
        for args in (("create", "g3"), ("list",), ("teardown", "g3", "--force")):
            run = ctx.tb(*args, *ctx.wt_args())
            ctx.exit_is(run, 2)
            ctx.check("host" in run.stderr and "docker" in run.stderr, "names valid values")
    finally:
        with_config(ctx, "")


@scenario("G4", "an explicit flag beats config")
def g4(ctx: Ctx) -> None:
    with_config(ctx, 'base = "dev"\n')
    try:
        ctx.eq(created(ctx, "cfg-override", "--base", "main").get("base"), "main", "base")
    finally:
        with_config(ctx, "")


@scenario("G5", "config harness; the record outlives the config")
def g5(ctx: Ctx) -> None:
    with_config(ctx, 'harness = "codex"\n')
    try:
        run = ctx.tb("create", "g5", *ctx.wt_args(), "--print")
        ctx.eq(host_launch_argv(run)[:1], ["codex"], "config harness")
    finally:
        with_config(ctx, "")
    again = ctx.tb("enter", "g5", *ctx.wt_args(), "--print")
    ctx.eq(host_launch_argv(again)[:1], ["codex"], "recorded harness after config reset")


@scenario("G6", "config root, repo-relative")
def g6(ctx: Ctx) -> None:
    with_config(ctx, 'root = "trees"\n')
    try:
        run = ctx.tb("create", "g6", "--repo", str(ctx.repo), "--json")
        ctx.exit_is(run, 0)
        ctx.eq(
            (ctx.json_out(run) or {}).get("worktree_path"), str(ctx.repo / "trees" / "g6"), "path"
        )
    finally:
        with_config(ctx, "")


@scenario("G7", "config env_file")
def g7(ctx: Ctx) -> None:
    secrets = ctx.sbx / "secrets.env"
    secrets.write_text("SECRET=from-config\n")
    with_config(ctx, f"env_file = {json.dumps(str(secrets))}\n")
    try:
        created(ctx, "g7")
        ctx.eq((wt(ctx, "g7") / ".env").read_text(), "SECRET=from-config\n", "worktree .env")
    finally:
        with_config(ctx, "")


@scenario("G8", "config setup_hook replaces auto-detected setup")
def g8(ctx: Ctx) -> None:
    with_config(ctx, 'setup_hook = ["echo hook-ran > hook.txt"]\n')
    try:
        created(ctx, "g8")
    finally:
        with_config(ctx, "")
    path = wt(ctx, "g8")
    hook = path / "hook.txt"
    ctx.check(hook.is_file() and hook.read_text() == "hook-ran\n", "hook ran in the worktree")
    ctx.check(not (path / ".venv").exists(), "uv setup did not run")


@scenario("G9", "config caches and XDG_CACHE_HOME")
def g9(ctx: Ctx) -> None:
    cache = ctx.sbx / "uv-cache"
    with_config(ctx, f"[caches]\nuv = {json.dumps(str(cache))}\n")
    try:
        created(ctx, "g9")
    finally:
        with_config(ctx, "")
    ctx.check(cache.is_dir() and any(cache.iterdir()), "uv used the configured cache")
    xdg = ctx.sbx / "xdg"
    run = ctx.tb(
        "create", "g9b", *ctx.wt_args(), "--json", env=dict(ctx.env, XDG_CACHE_HOME=str(xdg))
    )
    ctx.exit_is(run, 0)
    ctx.check((xdg / "uv").is_dir(), "default uv cache follows XDG_CACHE_HOME")


@scenario("G10", "config firewall and --firewall/--no-firewall")
def g10(ctx: Ctx) -> None:
    def plan(*extra: str) -> str:
        run = ctx.tb(
            "create",
            "e2e-fw",
            *ctx.wt_args(),
            "--isolation",
            "docker",
            *extra,
            "--dry-run",
            "--json",
        )
        ctx.exit_is(run, 0)
        return "\n".join((ctx.json_out(run) or {}).get("commands", []))

    with_config(ctx, "firewall = true\n")
    try:
        on = plan()
        off = plan("--no-firewall")
    finally:
        with_config(ctx, "")
    flag_on = plan("--firewall")
    for text, label in ((on, "config firewall"), (flag_on, "--firewall")):
        ctx.check("NET_ADMIN" in text and "init-firewall.sh" in text, f"{label} adds the firewall")
    ctx.check("NET_ADMIN" not in off and "init-firewall.sh" not in off, "--no-firewall wins")


@scenario("G11", "config template")
def g11(ctx: Ctx) -> None:
    with_config(ctx, 'template = "ghost"\n')
    try:
        run = ctx.tb("create", "e2e-g11", *ctx.wt_args(), "--isolation", "docker", "--json")
        ctx.exit_is(run, 3)
        ctx.json_err(run, "TEMPLATE_NOT_FOUND")
        ctx.check(not wt(ctx, "e2e-g11").exists(), "nothing provisioned")
    finally:
        with_config(ctx, "")


@scenario("G12", "unknown keys and wrong types are rejected")
def g12(ctx: Ctx) -> None:
    for text, needle in (
        ('isolatoin = "host"\n', "did you mean 'isolation'"),
        ("firewall = 1\n", "boolean"),
    ):
        with_config(ctx, text)
        try:
            run = ctx.tb("list", *ctx.wt_args(), "--json")
            ctx.exit_is(run, 2)
            err = ctx.json_err(run, "INVALID_CONFIG")
            ctx.check(needle in err.get("message", ""), f"message says {needle!r}")
        finally:
            with_config(ctx, "")


@scenario("G13", "TREEBOX_TEMPLATE_DIR and TZ")
def g13(ctx: Ctx) -> None:
    env = dict(ctx.env, TREEBOX_TEMPLATE_DIR=str(OFFLINE_TEMPLATE_DIR))
    run = ctx.tb("template", "list", "--json", env=env)
    ctx.exit_is(run, 0)
    ctx.eq(
        (ctx.json_out(run) or {}).get("templateDirOverride"), str(OFFLINE_TEMPLATE_DIR), "override"
    )
    path = ctx.tb("template", "path", "any-name", env=env)
    ctx.eq(path.stdout.strip(), str(OFFLINE_TEMPLATE_DIR), "override wins for any name")
    tz = ctx.tb(
        "create",
        "e2e-tz",
        *ctx.wt_args(),
        "--isolation",
        "docker",
        "--dry-run",
        "--json",
        env=dict(ctx.env, TZ="Europe/Helsinki"),
    )
    ctx.exit_is(tz, 0)
    commands = "\n".join((ctx.json_out(tz) or {}).get("commands", []))
    ctx.check("TZ=Europe/Helsinki" in commands, "host TZ reaches the image build")


@scenario("G14", "[ports]: blocks never overlap, a full range is PORTS_EXHAUSTED")
def g14(ctx: Ctx) -> None:
    with_config(ctx, "[ports]\nstart = 30000\nend = 30019\nblock_size = 10\n")
    try:
        bases = [created(ctx, name).get("port_base") for name in ("g14-a", "g14-b")]
        ctx.eq(bases, [30000, 30010], "one block each, lowest free first")
        full = ctx.tb("create", "g14-c", *ctx.wt_args(), "--json")
        ctx.exit_is(full, 5)
        err = ctx.json_err(full, "PORTS_EXHAUSTED")
        ctx.eq(err.get("path"), str(ctx.fx.treebox_home / "ports.json"), "error.path is the ledger")
        ctx.check(not wt(ctx, "g14-c").exists() and not branch_exists(ctx, "g14-c"), "nothing made")
        ctx.exit_is(ctx.tb("teardown", "g14-a", *ctx.wt_args(), "--force", "--json"), 0)
        ctx.eq(created(ctx, "g14-c").get("port_base"), 30000, "teardown released the block")
        enter = ctx.tb("enter", "g14-b", *ctx.wt_args(), "--json")
        ctx.eq((ctx.json_out(enter) or {}).get("port_base"), 30010, "the block is stable on enter")
    finally:
        with_config(ctx, "")


@scenario("G15", "PORT_LEDGER_INVALID: treebox never rewrites a broken ledger")
def g15(ctx: Ctx) -> None:
    ledger = ctx.fx.treebox_home / "ports.json"
    ledger.write_text("not json\n")
    run = ctx.tb("create", "g15", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 1)
    err = ctx.json_err(run, "PORT_LEDGER_INVALID")
    ctx.eq(err.get("path"), str(ledger), "error.path")
    ctx.eq(ledger.read_text(), "not json\n", "the ledger is untouched")
    ctx.check(not wt(ctx, "g15").exists(), "nothing made")


@scenario("G16", "[resources] config: the default limits of a new sandbox")
def g16(ctx: Ctx) -> None:
    with_config(ctx, '[resources]\nmemory = "2g"\npids = 128\n')
    try:
        plan = ctx.tb(
            "create", "e2e-g16", *ctx.wt_args(), "--isolation", "docker", "--dry-run", "--json"
        )
        ctx.exit_is(plan, 0)
        d = ctx.json_out(plan) or {}
        ctx.eq(d.get("resources"), {"cpus": None, "memory": "2g", "pids": 128}, "resources")
        run_cmd = next((c for c in d.get("commands", []) if c.startswith("docker run ")), "")
        ctx.check("--memory 2g --pids-limit 128" in run_cmd, "docker run carries the config limits")
        over = ctx.tb(
            "create",
            "e2e-g16",
            *ctx.wt_args(),
            "--isolation",
            "docker",
            "--memory",
            "1g",
            "--dry-run",
            "--json",
        )
        ctx.eq((ctx.json_out(over) or {}).get("resources", {}).get("memory"), "1g", "flag wins")
        host = ctx.tb("create", "g16-host", *ctx.wt_args(), "--json")
        ctx.exit_is(host, 0)
        ctx.eq(
            (ctx.json_out(host) or {}).get("resources"),
            {"cpus": None, "memory": None, "pids": None},
            "config limits never refuse a host worktree; it records none",
        )
        bad = '[resources]\nmemory = "lots"\n'
        with_config(ctx, bad)
        invalid = ctx.tb("list", *ctx.wt_args(), "--json")
        ctx.exit_is(invalid, 2)
        ctx.json_err(invalid, "INVALID_CONFIG")
    finally:
        with_config(ctx, "")


# --- H. docker isolation (real engine, offline template) ----------------------------------------


@group("H", "docker isolation (real engine, offline template)", requires=DOCKER)
def _setup_h(ctx: Ctx) -> None:
    ctx.fx.install_offline_template()


def docker_args(ctx: Ctx) -> list[str]:
    return [*ctx.wt_args(), "--isolation", "docker", "--template", OFFLINE_TEMPLATE]


def container(ctx: Ctx, name: str) -> tuple[str, str]:
    """(container name, running) for a worktree, found by treebox's label."""
    out = ctx.sh(
        "docker",
        "ps",
        "-a",
        "--filter",
        f"label=treebox.workspace={wt(ctx, name)}",
        "--format",
        "{{.Names}} {{.State}}",
    ).stdout.split()
    return (out[0], out[1]) if len(out) >= 2 else ("", "")


@scenario("H1", "docker create provisions host-side and sets up the container")
def h1(ctx: Ctx) -> None:
    run = ctx.tb("create", "e2e-dockered", *docker_args(ctx), "--print")
    ctx.exit_is(run, 0)
    argv = printed_argv(run)
    ctx.eq(argv[:2], ["docker", "exec"], "launch command targets the container")
    path = wt(ctx, "e2e-dockered")
    ctx.check(path.is_dir(), "worktree")
    ctx.check((path / ".env").read_text() == "SECRET=canonical\n", ".env copied host-side")
    ctx.eq(ctx.git("branch", "--show-current", cwd=path), "e2e-dockered", "branch")
    name, state = container(ctx, "e2e-dockered")
    ctx.check(name.startswith("treebox-e2e-dockered-"), f"container name {name!r}")
    ctx.eq(state, "running", "container state")
    marker = ctx.sh("docker", "exec", name, "cat", "/tmp/treebox-e2e-post-create", check=False)
    ctx.eq(marker.stdout, f"uid={os.getuid()}\ncwd={path}\n", "post-create ran as the host uid")


@scenario("H2", "sandbox config and staged logins live outside the mount")
def h2(ctx: Ctx) -> None:
    created(ctx, "e2e-dockered", *docker_args(ctx)[2:])
    cfg = ctx.root / ".containers" / "e2e-dockered"
    path = wt(ctx, "e2e-dockered")
    ctx.check((cfg / "container.json").is_file(), "rendered container.json beside the worktree")
    ctx.check((cfg / "Dockerfile").is_file(), "build context beside the worktree")
    ctx.check(not (path / "container.json").exists(), "nothing rendered inside the worktree")
    staged = cfg / "credentials" / "claude" / ".credentials.json"
    real = ctx.fx.home / ".claude" / ".credentials.json"
    ctx.check(staged.is_file() and not staged.is_symlink(), "staged login is a regular file")
    ctx.check(staged.read_text() == real.read_text(), "staged copy matches the (fake) login")
    ctx.check(staged.stat().st_ino != real.stat().st_ino, "a copy, not a hardlink")
    mounts = ctx.sh(
        "docker",
        "inspect",
        container(ctx, "e2e-dockered")[0],
        "--format",
        "{{range .Mounts}}{{.Source}}\n{{end}}",
    ).stdout
    ctx.check(str(ctx.fx.home / ".claude") not in mounts, "the live ~/.claude is not mounted")


@scenario("H3", "an unknown template is loud and leaves nothing")
def h3(ctx: Ctx) -> None:
    args = ["create", "e2e-tpl-ghost", *ctx.wt_args(), "--isolation", "docker"]
    for attempt in (1, 2):
        run = ctx.tb(*args, "--template", "ghost", "--json")
        ctx.exit_is(run, 3)
        err = ctx.json_err(run, "TEMPLATE_NOT_FOUND")
        ctx.check("ghost" in err.get("message", ""), f"attempt {attempt} names the template")
    ctx.check(not wt(ctx, "e2e-tpl-ghost").exists(), "no worktree")
    ctx.check(not branch_exists(ctx, "e2e-tpl-ghost"), "no branch")
    ctx.check(not (ctx.root / ".containers" / "e2e-tpl-ghost").exists(), "no sandbox files")
    ctx.eq(container(ctx, "e2e-tpl-ghost")[0], "", "container")


@scenario("H4", "recorded docker isolation is reused")
def h4(ctx: Ctx) -> None:
    created(ctx, "e2e-dockered", *docker_args(ctx)[2:])
    run = ctx.tb("enter", "e2e-dockered", *ctx.wt_args(), "--print")
    ctx.exit_is(run, 0)
    ctx.eq(printed_argv(run)[:2], ["docker", "exec"], "still docker")


@scenario("H5", "a stopped container is revived before the command is printed")
def h5(ctx: Ctx) -> None:
    created(ctx, "e2e-dockered", *docker_args(ctx)[2:])
    name, _ = container(ctx, "e2e-dockered")
    ctx.sh("docker", "stop", "-t", "1", name)
    ctx.eq(container(ctx, "e2e-dockered")[1], "exited", "stopped")
    run = ctx.tb("enter", "e2e-dockered", *ctx.wt_args(), "--print")
    ctx.exit_is(run, 0)
    ctx.eq(container(ctx, "e2e-dockered")[1], "running", "running after enter --print")
    replay = ctx.sh(*printed_argv(run), check=False)
    ctx.eq(replay.exit, 0, "the printed command replays")
    rec = stub_record(replay.stdout) or {}
    ctx.eq(rec.get("in_container"), "yes", "replay reached the stub in the container")


@scenario("H6", "docker teardown, --remove-volumes, --skip-container")
def h6(ctx: Ctx) -> None:
    created(ctx, "e2e-dockered", *docker_args(ctx)[2:])
    run = ctx.tb("teardown", "e2e-dockered", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(run, 0)
    rec = ((ctx.json_out(run) or {}).get("worktrees") or [{}])[0]
    ctx.eq(rec.get("container"), "cleaned", "container")
    ctx.eq(container(ctx, "e2e-dockered")[0], "", "container gone")
    ctx.check(not (ctx.root / ".containers" / "e2e-dockered").exists(), "sandbox files gone")
    created(ctx, "e2e-dockered2", *docker_args(ctx)[2:])
    vol = ctx.tb(
        "teardown", "e2e-dockered2", *ctx.wt_args(), "--force", "--remove-volumes", "--json"
    )
    ctx.exit_is(vol, 0)
    rec = ((ctx.json_out(vol) or {}).get("worktrees") or [{}])[0]
    ctx.eq(rec.get("volumes_removed"), True, "volumes_removed")
    gone = ctx.sh("docker", "volume", "inspect", "treebox-e2e-vol-e2e-dockered2", check=False)
    ctx.check(gone.exit != 0, "per-workspace volume removed")
    created(ctx, "e2e-dockered3", *docker_args(ctx)[2:])
    skip = ctx.tb(
        "teardown", "e2e-dockered3", *ctx.wt_args(), "--force", "--skip-container", "--json"
    )
    ctx.exit_is(skip, 0)
    rec = ((ctx.json_out(skip) or {}).get("worktrees") or [{}])[0]
    ctx.eq(rec.get("container"), "skipped", "container")
    ctx.eq(container(ctx, "e2e-dockered3")[1], "running", "container left running")
    ctx.check(not wt(ctx, "e2e-dockered3").exists(), "worktree removed")


@scenario("H7", "real launch in the container runs the stub and returns its status")
def h7(ctx: Ctx) -> None:
    run = ctx.tb("create", "e2e-launch", *docker_args(ctx))
    ctx.exit_is(run, 0)
    rec = stub_record(run.stdout) or {}
    ctx.eq(rec.get("in_container"), "yes", "agent ran in the container")
    ctx.eq(rec.get("cwd"), str(wt(ctx, "e2e-launch")), "agent cwd is the mounted worktree")
    ctx.eq(rec.get("uid"), str(os.getuid()), "agent uid is the host uid")
    ctx.eq(rec.get("api_key"), "unset", "ANTHROPIC_API_KEY")
    row = listed(ctx).get("e2e-launch", {})
    ctx.eq(
        (rec.get("treebox_name"), rec.get("treebox_id"), rec.get("treebox_port_base")),
        ("e2e-launch", row.get("id"), str(row.get("port_base"))),
        "worktree identity in the container's agent environment",
    )
    again = ctx.tb(
        "enter", "e2e-launch", *ctx.wt_args(), "--", "--stub-exit=3", tags=("agent-exit",)
    )
    ctx.exit_is(again, 3)
    ctx.eq(
        (stub_record(again.stdout) or {}).get("args"),
        ["--dangerously-skip-permissions", "--stub-exit=3"],
        "argv",
    )


@scenario("H8", "in-container git works; shared hooks are read-only")
def h8(ctx: Ctx) -> None:
    created(ctx, "e2e-launch", *docker_args(ctx)[2:])
    name, _ = container(ctx, "e2e-launch")
    path = str(wt(ctx, "e2e-launch"))
    branch = ctx.sh(
        "docker",
        "exec",
        "-u",
        "agent",
        "-w",
        path,
        name,
        "git",
        "branch",
        "--show-current",
        check=False,
    )
    ctx.eq(branch.stdout.strip(), "e2e-launch", "in-container git branch")
    status = ctx.sh(
        "docker",
        "exec",
        "-u",
        "agent",
        "-w",
        path,
        name,
        "git",
        "status",
        "--porcelain",
        check=False,
    )
    ctx.eq(status.exit, 0, "in-container git status exit")
    hooks = ctx.repo / ".git" / "hooks"
    touch = ctx.sh(
        "docker", "exec", "-u", "agent", name, "touch", str(hooks / "planted"), check=False
    )
    ctx.check(touch.exit != 0 and not (hooks / "planted").exists(), "hooks dir is read-only")


@scenario("H9", "a missing container is a clean error, not a dead command")
def h9(ctx: Ctx) -> None:
    created(ctx, "e2e-gone", *docker_args(ctx)[2:], root=ctx.root)
    name, _ = container(ctx, "e2e-gone")
    ctx.sh("docker", "rm", "-f", name)
    run = ctx.tb("enter", "e2e-gone", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 1)
    err = ctx.json_err(run, "ERROR")
    ctx.check("No container exists" in err.get("message", ""), "message says why")


@scenario(
    "H10", "injected engine failure: an incomplete teardown fails and retries", evidence="boundary"
)
def h10(ctx: Ctx) -> None:
    created(ctx, "e2e-gone", *docker_args(ctx)[2:])
    ctx.sh("docker", "rm", "-f", container(ctx, "e2e-gone")[0])
    docker = shutil.which("docker", path=ctx.env["PATH"]) or ""
    env = dict(ctx.env, TREEBOX_E2E_DOCKER_FAIL="rm", TREEBOX_E2E_REAL_DOCKER=docker)
    env["PATH"] = os.pathsep.join([str(ctx.fx.stub_bin), str(FAKES_DIR), ctx.env["PATH"]])
    run = ctx.tb("teardown", "e2e-gone", *ctx.wt_args(), "--force", "--json", env=env)
    ctx.exit_is(run, 0)
    created(ctx, "e2e-fail-rm", *docker_args(ctx)[2:])
    name = container(ctx, "e2e-fail-rm")[0]
    failed = ctx.tb("teardown", "e2e-fail-rm", *ctx.wt_args(), "--force", "--json", env=env)
    ctx.exit_is(failed, 1)
    payload = ctx.json_out(failed) if failed.stdout.strip() else None
    records = (payload or {}).get("worktrees", [])
    rec = records[0] if records else {}
    ctx.eq((rec.get("container"), rec.get("complete")), ("failed", False), "record")
    ctx.check(any(name in r for r in rec.get("remaining", [])), f"remaining names {name}")
    ctx.check("TEARDOWN_INCOMPLETE" in failed.stderr, "stderr carries TEARDOWN_INCOMPLETE")
    manifest = ctx.root / ".containers" / "e2e-fail-rm" / "treebox-resources.json"
    kept = wt(ctx, "e2e-fail-rm").is_dir() and record_file(ctx, "e2e-fail-rm").is_file()
    ctx.check(kept and manifest.is_file(), "worktree, record and resource manifest kept")
    retry = ctx.tb("teardown", "e2e-fail-rm", *ctx.wt_args(), "--force", "--json")
    ctx.exit_is(retry, 0)
    ctx.eq(container(ctx, "e2e-fail-rm")[0], "", "the retry removed the container")
    gone = not wt(ctx, "e2e-fail-rm").exists() and not record_file(ctx, "e2e-fail-rm").exists()
    ctx.check(gone and not manifest.exists(), "nothing left after the retry")


@scenario("H11", "enter --template overrides the recorded template for one session")
def h11(ctx: Ctx) -> None:
    created(ctx, "e2e-launch", *docker_args(ctx)[2:])
    run = ctx.tb("enter", "e2e-launch", *ctx.wt_args(), "--template", OFFLINE_TEMPLATE, "--print")
    ctx.exit_is(run, 0)
    ghost = ctx.tb("enter", "e2e-launch", *ctx.wt_args(), "--template", "ghost", "--json")
    ctx.exit_is(ghost, 0)
    ctx.note("an explicit --template ghost on enter still enters the existing container")


def inspect(ctx: Ctx, name: str, fmt: str) -> str:
    return ctx.sh("docker", "inspect", container(ctx, name)[0], "--format", fmt).stdout.strip()


@scenario("H12", "resource limits reach the engine; enter only restates them")
def h12(ctx: Ctx) -> None:
    limits = ["--cpus", "1", "--memory", "512m", "--pids-limit", "64"]
    d = created(ctx, "e2e-lim", *docker_args(ctx)[2:], *limits)
    ctx.eq(d.get("resources"), {"cpus": 1, "memory": "512m", "pids": 64}, "create resources")
    host_config = "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}}"
    ctx.eq(inspect(ctx, "e2e-lim", host_config), "536870912 1000000000 64", "docker inspect")
    ctx.eq(listed(ctx).get("e2e-lim", {}).get("resources"), d.get("resources"), "list resources")
    same = ctx.tb("enter", "e2e-lim", *ctx.wt_args(), "--memory", "512m", "--json")
    ctx.exit_is(same, 0)
    other = ctx.tb("enter", "e2e-lim", *ctx.wt_args(), "--memory", "1g", "--json")
    ctx.exit_is(other, 5)
    hint = ctx.json_err(other, "RESOURCE_MISMATCH").get("hint", "")
    ctx.check("treebox teardown" in hint and "treebox create" in hint, "hint re-creates")
    with_config(ctx, '[resources]\nmemory = "2g"\n')
    try:
        created(ctx, "e2e-lim-cfg", *docker_args(ctx)[2:])
        with_config(ctx, '[resources]\nmemory = "3g"\n')
        drift = ctx.json_out(ctx.tb("enter", "e2e-lim-cfg", *ctx.wt_args(), "--json")) or {}
        ctx.eq((drift.get("resources_drift") or {}).get("memory"), "3g", "config drift reported")
        ctx.eq(len(drift.get("warnings", [])), 1, "one drift warning")
        steady = ctx.json_out(ctx.tb("enter", "e2e-lim", *ctx.wt_args(), "--json")) or {}
        values = set((steady.get("resources_drift") or {}).values())
        ctx.eq(values, {None}, "a flag-chosen limit never drifts")
    finally:
        with_config(ctx, "")


@scenario(
    "H13",
    "the macOS Keychain login is staged into the sandbox",
    requires=(*DOCKER, "macos"),
    evidence="boundary",
)
def h13(ctx: Ctx) -> None:
    (ctx.fx.home / ".claude" / ".credentials.json").unlink()
    login = '{"keychain": "treebox-e2e-staged"}'
    env = fake_keychain(ctx, login)
    run = ctx.tb("create", "e2e-kc", *docker_args(ctx), "--json", env=env)
    ctx.exit_is(run, 0)
    staged = ctx.root / ".containers" / "e2e-kc" / "credentials" / "claude" / ".credentials.json"
    ctx.check(staged.is_file() and not staged.is_symlink(), "staged as a regular file")
    ctx.eq(staged.read_text().strip() if staged.is_file() else "", login, "staged content")
    ctx.eq(staged.stat().st_mode & 0o077 if staged.is_file() else -1, 0, "private to the user")
    name = container(ctx, "e2e-kc")[0]
    inside = ctx.sh(
        "docker",
        "exec",
        "-u",
        "agent",
        name,
        "cat",
        "/home/agent/.claude/.credentials.json",
        check=False,
    )
    ctx.eq(inside.stdout.strip(), login, "the sandbox sees the login file")


def firewall_template(ctx: Ctx) -> str:
    """The offline template plus a firewall overlay whose init script only
    counts its runs (and fails on demand): no iptables, no network. It proves
    when treebox runs the firewall, not what a real firewall blocks."""
    name = "e2e-fw"
    dest = ctx.fx.treebox_home / "templates" / name
    shutil.copytree(OFFLINE_TEMPLATE_DIR, dest, symlinks=False)
    (dest / "firewall.json").write_text('{"env": {"TREEBOX_FIREWALL": "1"}}\n')
    (dest / "init-firewall.sh").write_text(
        "#!/bin/sh\n"
        "if [ -f /tmp/treebox-e2e-firewall-fail ]; then\n"
        '  echo "ERROR: injected firewall failure (treebox e2e)"; exit 1\n'
        "fi\n"
        "when=before-setup\n"
        "[ -f /tmp/treebox-e2e-post-create ] && when=after-setup\n"
        'echo "$when" >> /tmp/treebox-e2e-firewall-runs\n'
    )
    with (dest / "Dockerfile").open("a") as f:
        f.write(
            "COPY init-firewall.sh /usr/local/bin/init-firewall.sh\n"
            "RUN chmod 0755 /usr/local/bin/init-firewall.sh\n"
        )
    return name


@scenario("H14", "the firewall runs before setup and on every entry; a failure is FIREWALL_FAILED")
def h14(ctx: Ctx) -> None:
    template = firewall_template(ctx)
    args = [*ctx.wt_args(), "--isolation", "docker", "--template", template, "--firewall"]
    created(ctx, "e2e-fw", *args[2:])
    name = container(ctx, "e2e-fw")[0]

    def log() -> list[str]:
        out = ctx.sh("docker", "exec", name, "cat", "/tmp/treebox-e2e-firewall-runs", check=False)
        return out.stdout.split()

    def runs() -> int:
        return len(log())

    after_create = runs()
    ctx.eq(log()[:1], ["before-setup"], "the first run precedes the post-create setup")
    ctx.exit_is(ctx.tb("enter", "e2e-fw", *ctx.wt_args(), "--print"), 0)
    ctx.eq(runs(), after_create + 1, "an entry to a running container re-applies it")
    ctx.sh("docker", "restart", "-t", "1", name)
    ctx.exit_is(ctx.tb("enter", "e2e-fw", *ctx.wt_args(), "--print"), 0)
    ctx.eq(runs(), after_create + 2, "the entry after a restart applies it again")
    ctx.sh("docker", "exec", name, "touch", "/tmp/treebox-e2e-firewall-fail")
    failed = ctx.tb("enter", "e2e-fw", *ctx.wt_args(), "--json")
    ctx.exit_is(failed, 1)
    err = ctx.json_err(failed, "FIREWALL_FAILED")
    ctx.check(
        "injected firewall failure" in err.get("message", ""), "message has the script's error"
    )
    ctx.check(f"treebox template path {template}" in err.get("hint", ""), "hint names the template")
    ctx.eq(failed.stdout, "", "no entry command is emitted")
    launch = ctx.tb("enter", "e2e-fw", *ctx.wt_args())
    ctx.exit_is(launch, 1)
    ctx.check(stub_record(launch.stdout) is None, "the agent is not launched")


def wait_for_stub(ctx: Ctx, bg: Background, timeout: float = 60) -> dict[str, Any] | None:
    """The stub's launch record once a background launch has printed it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and bg.running():
        rec = stub_record(bg.stdout_so_far())
        if rec is not None:
            return rec
        time.sleep(0.2)
    return stub_record(bg.stdout_so_far())


@scenario("H15", "a sandbox restarted under the agent is a runtime error, not an agent exit")
def h15(ctx: Ctx) -> None:
    created(ctx, "e2e-rst", *docker_args(ctx)[2:])
    name = container(ctx, "e2e-rst")[0]
    bg = ctx.tb_background("enter", "e2e-rst", *ctx.wt_args(), "--", "--stub-sleep=60")
    ctx.check(wait_for_stub(ctx, bg) is not None, "the agent runs in the container")
    ctx.sh("docker", "restart", "-t", "1", name)
    run = bg.wait(timeout=120)
    ctx.exit_is(run, 1)
    ctx.check("stopped or restarted" in run.stderr, "says the sandbox stopped or restarted")
    ctx.check("agent exited with status" not in run.stderr, "no agent exit line")


@scenario("H16", "a harness binary missing from the image is HARNESS_MISSING at launch")
def h16(ctx: Ctx) -> None:
    created(ctx, "e2e-nocodex", *docker_args(ctx)[2:])
    name = container(ctx, "e2e-nocodex")[0]
    ctx.sh("docker", "exec", "-u", "root", name, "rm", "/usr/local/bin/codex")
    run = ctx.tb("enter", "e2e-nocodex", *ctx.wt_args(), "--harness", "codex")
    ctx.exit_is(run, 1)
    ctx.check("'codex' not found in the sandbox" in run.stderr, "message names the binary")
    ctx.check("agent exited with status" not in run.stderr, "a treebox failure, not an agent exit")
    ok = ctx.tb("enter", "e2e-nocodex", *ctx.wt_args(), tags=("agent-exit",))
    ctx.exit_is(ok, 0)


@scenario("H17", "cancel stops the agent inside the container; the container keeps running")
def h17(ctx: Ctx) -> None:
    created(ctx, "e2e-cancel", *docker_args(ctx)[2:])
    name = container(ctx, "e2e-cancel")[0]
    bg = ctx.tb_background(
        "enter", "e2e-cancel", *ctx.wt_args(), "--", "--stub-sleep=60", tags=("agent-exit",)
    )
    ctx.check(wait_for_stub(ctx, bg) is not None, "the agent runs in the container")
    bg.signal(signal.SIGTERM)
    run = bg.wait(timeout=60)
    ctx.exit_is(run, 143)
    ctx.check("Cancelled by SIGTERM" in run.stderr, "says it was cancelled")
    ctx.check("agent exited with status" not in run.stderr, "no agent exit line")
    top = ctx.sh("docker", "top", name, check=False).stdout
    ctx.check("sleep 60" not in top, "the agent is gone from the container")
    ctx.eq(container(ctx, "e2e-cancel")[1], "running", "the container keeps running")


@scenario(
    "H18",
    "the bundled image: agents, Chromium without its own sandbox, a real firewall",
    requires=("bundled-image",),
)
def h18(ctx: Ctx) -> None:
    args = [*ctx.wt_args(), "--isolation", "docker", "--firewall"]
    run = ctx.tb("create", "e2e-bundled", *args, "--print", timeout=3600)
    ctx.exit_is(run, 0)
    name = container(ctx, "e2e-bundled")[0]

    def agent(*cmd: str) -> RunRecord:
        return ctx.sh("docker", "exec", "-u", "agent", name, *cmd, check=False)

    def rules() -> str:
        return ctx.sh("docker", "exec", "-u", "root", name, "iptables", "-S", "OUTPUT").stdout

    for binary in ("claude", "codex", "uv", "gh"):
        ctx.eq(agent("sh", "-c", f"command -v {binary}").exit, 0, f"{binary} in the image")
    chrome = agent(
        "/opt/google/chrome/chrome", "--headless", "--no-sandbox", "--dump-dom", "about:blank"
    )
    ctx.eq(chrome.exit, 0, "Chromium runs headless in the container")
    ctx.check("No usable sandbox" not in chrome.stderr, "Chromium needs no sandbox of its own")
    blocked = agent("curl", "-sS", "--max-time", "5", "https://example.com")
    ctx.check(blocked.exit != 0, "egress to a host outside the allowlist is blocked")
    ctx.check("REJECT" in rules(), "the egress lockdown is in place")
    route = ctx.sh("docker", "exec", name, "ip", "route", check=False).stdout.split()
    gateway = route[route.index("via") + 1] if "via" in route else ""
    ctx.check(
        bool(gateway) and f"-d {gateway}/32 -j ACCEPT" not in rules(),
        f"no blanket exception for the host gateway {gateway} (ALLOWED_HOST_PORTS is empty)",
    )
    ctx.sh("docker", "restart", "-t", "1", name)
    ctx.check("REJECT" not in rules(), "a restart outside treebox drops the rules")
    ctx.exit_is(ctx.tb("enter", "e2e-bundled", *ctx.wt_args(), "--print", timeout=600), 0)
    ctx.check("REJECT" in rules(), "the next entry re-applies the lockdown")


# --- I. interactive routes (pseudo-terminal) -----------------------------------------------------


@group("I", "interactive prompts on a terminal", requires=("pty",))
def _setup_i(ctx: Ctx) -> None:
    for name in ("i-keep", "i-yes", "i-pick", "i-stay", "i-dirty"):
        created(ctx, name)


@scenario("I1", "confirmation prompt: no keeps the worktree")
def i1(ctx: Ctx) -> None:
    with ctx.pty("teardown", "i-keep", *ctx.wt_args()) as term:
        ctx.check(term.expect(r"Remove worktree i-keep\?"), "prompt shown")
        term.send("n\r")
        code = term.wait()
    ctx.eq(code, 1, "exit status after answering no")
    ctx.check("Aborted" in term.screen_text(), "says aborted")
    ctx.check(wt(ctx, "i-keep").is_dir(), "worktree kept")


@scenario("I2", "confirmation prompt: yes removes the worktree")
def i2(ctx: Ctx) -> None:
    with ctx.pty("teardown", "i-yes", *ctx.wt_args()) as term:
        ctx.check(term.expect(r"Remove worktree i-yes\?"), "prompt shown")
        term.send("y\r")
        code = term.wait()
    ctx.eq(code, 0, "exit status after answering yes")
    ctx.check(not wt(ctx, "i-yes").exists(), "worktree removed")
    ctx.check(branch_exists(ctx, "i-yes"), "branch kept")


def chooser_rows(text: str, names: list[str]) -> list[str]:
    """The worktree names in the order the chooser shows them."""
    order: list[str] = []
    for line in text.splitlines():
        for name in names:
            if re.search(rf"(^|\s){re.escape(name)}\s", line) and name not in order:
                order.append(name)
    return order


def pick(
    ctx: Ctx, targets: list[str], *, delete_branch: list[str] | None = None
) -> tuple[int, str]:
    """Drive the teardown chooser: tick ``targets``, then answer the branch
    question by ticking ``delete_branch``."""
    names = sorted(p.name for p in ctx.root.iterdir() if not p.name.startswith("."))
    with ctx.pty("teardown", *ctx.wt_args(), rows=40, cols=140) as term:
        if not ctx.check(term.expect(r"Select worktrees to tear down"), "chooser shown"):
            term.send("\x03")
            return term.wait(), term.screen_text()
        time.sleep(0.5)
        term.expect(r"$", timeout=1)
        order = chooser_rows(term.screen_text(), names)
        ctx.check(all(t in order for t in targets), f"chooser lists the targets (saw {order})")
        position = 0
        for target in sorted(targets, key=order.index):
            index = order.index(target)
            term.send("\x1b[B" * (index - position) + " ")
            position = index
            time.sleep(0.2)
        term.send("\r")
        if ctx.check(term.expect(r"Also delete the local branch", timeout=30), "branch question"):
            time.sleep(0.5)
            subset = [t for t in order if t in targets]
            position = 0
            for target in delete_branch or []:
                index = subset.index(target)
                term.send("\x1b[B" * (index - position) + " ")
                position = index
                time.sleep(0.2)
            term.send("\r")
        code = term.wait(timeout=120)
    return code, term.screen_text()


@scenario("I3", "chooser removes the picked worktree only")
def i3(ctx: Ctx) -> None:
    code, _ = pick(ctx, ["i-pick"])
    ctx.eq(code, 0, "exit status")
    ctx.check(not wt(ctx, "i-pick").exists(), "picked worktree removed")
    ctx.check(branch_exists(ctx, "i-pick"), "its branch kept (none ticked)")
    for name in ("i-keep", "i-stay", "i-dirty"):
        ctx.check(wt(ctx, name).is_dir(), f"{name} untouched")


@scenario("I4", "chooser Ctrl+C removes nothing")
def i4(ctx: Ctx) -> None:
    before = sorted(p.name for p in ctx.root.iterdir())
    with ctx.pty("teardown", *ctx.wt_args()) as term:
        ctx.check(term.expect(r"Select worktrees to tear down"), "chooser shown")
        time.sleep(0.5)
        term.send("\x03")
        code = term.wait()
    ctx.eq(code, 0, "exit status after cancel")
    ctx.eq(sorted(p.name for p in ctx.root.iterdir()), before, "worktrees")


@scenario("I5", "chooser keeps a dirty pick, removes the clean one, deletes its branch")
def i5(ctx: Ctx) -> None:
    (wt(ctx, "i-dirty") / "wip.txt").write_text("work in progress\n")
    code, text = pick(ctx, ["i-dirty", "i-stay"], delete_branch=["i-stay"])
    ctx.eq(code, 5, "exit status with a kept dirty worktree")
    ctx.check(wt(ctx, "i-dirty").is_dir(), "dirty worktree kept")
    ctx.check(not wt(ctx, "i-stay").exists(), "clean pick removed")
    ctx.check(not branch_exists(ctx, "i-stay"), "ticked branch deleted")
    ctx.check("uncommitted changes" in text, "says why it kept one")


# --- P. parallel dispatch ------------------------------------------------------------------------


@group("P", "parallel dispatch")
def _setup_p(ctx: Ctx) -> None:
    return


@scenario("P1", "parallel creates of different names all succeed")
def p1(ctx: Ctx) -> None:
    names = ["par-a", "par-b", "par-c", "par-d"]
    runs = ctx.tb_parallel([["create", n, *ctx.wt_args(), "--json"] for n in names])
    for run in runs:
        ctx.exit_is(run, 0)
    rows = listed(ctx)
    for name in names:
        ctx.eq(rows.get(name, {}).get("deps"), "fresh", f"{name} deps")
        ctx.eq(rows.get(name, {}).get("env"), "present", f"{name} env")


@scenario("P2", "parallel creates of the same name: one wins, one conflicts")
def p2(ctx: Ctx) -> None:
    runs = ctx.tb_parallel(
        [["create", "par-same", *ctx.wt_args(), "--json"]] * 2, tags=("lock-expected",)
    )
    codes = sorted(r.exit for r in runs)
    ctx.eq(codes, [0, 5], "exit statuses")
    loser = next((r for r in runs if r.exit == 5), None)
    if loser is not None:
        code = ctx.json_err(loser).get("code")
        ok = code in ("LOCK_HELD", "BRANCH_EXISTS", "SLUG_CONFLICT")
        ctx.check(ok, f"loser's error.code is a conflict (got {code})")
    lines = ctx.git("worktree", "list", "--porcelain").splitlines()
    registered = [ln for ln in lines if ln == f"worktree {wt(ctx, 'par-same')}"]
    ctx.eq(len(registered), 1, "registrations of par-same")


@scenario("P3", "a held worktree lock blocks enter and a batch teardown")
def p3(ctx: Ctx) -> None:
    created(ctx, "par-free")
    # A real second treebox holds the lock: a slow setup hook keeps
    # `create par-held` inside provisioning for a few seconds.
    ctx.write_config('setup_hook = ["sleep 6"]\n')
    try:
        bg = ctx.tb_background("create", "par-held", *ctx.wt_args(), "--print")
        lock = ctx.root / ".locks" / "par-held.lock"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (wt(ctx, "par-held") / ".env").exists():
            time.sleep(0.1)
        ctx.check(_locked(lock), "the background create holds the lock")
        enter = ctx.tb("enter", "par-held", *ctx.wt_args(), "--json", tags=("lock-expected",))
        ctx.exit_is(enter, 5)
        ctx.json_err(enter, "LOCK_HELD")
        batch = ctx.tb(
            "teardown",
            "par-free",
            "par-held",
            *ctx.wt_args(),
            "--force",
            "--json",
            tags=("lock-expected",),
        )
        ctx.exit_is(batch, 5)
        ctx.json_err(batch, "LOCK_HELD")
        ctx.check(wt(ctx, "par-free").is_dir(), "the unlocked target was not removed either")
        ctx.exit_is(bg.wait(), 0)
    finally:
        ctx.write_config("")
    after = ctx.tb("enter", "par-held", *ctx.wt_args(), "--print")
    ctx.exit_is(after, 0)


def _locked(lock: Path) -> bool:
    if not lock.exists():
        return False
    with open(lock, "w") as fd:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return False


# --- R. regressions for the bugs fixed on 11 November 2026 ---------------------------------------


@group("R", "regressions (#37 #38 #39 #43 #44 #45 #46)")
def _setup_r(ctx: Ctx) -> None:
    ctx.fx.install_offline_template()


@scenario("R44", "credential refresh never writes through a staged link", requires=DOCKER)
def r44(ctx: Ctx) -> None:
    created(ctx, "e2e-cred", *docker_args(ctx)[2:])
    staged = ctx.root / ".containers" / "e2e-cred" / "credentials" / "claude" / "settings.json"
    victim = ctx.sbx / "outside-victim"
    for kind in ("symlink", "hardlink"):
        victim.write_text("host data\n")
        victim.chmod(0o644)
        staged.unlink()
        if kind == "symlink":
            staged.symlink_to(victim)
        else:
            os.link(victim, staged)
        run = ctx.tb("enter", "e2e-cred", *ctx.wt_args(), "--print")
        ctx.exit_is(run, 0)
        ctx.eq(victim.read_text(), "host data\n", f"{kind} target content")
        ctx.eq(victim.stat().st_mode & 0o777, 0o644, f"{kind} target mode")
        ctx.check(staged.is_file() and not staged.is_symlink(), f"{kind} replaced by a file")
        ctx.eq(staged.read_text(), '{"treebox-e2e": true}\n', f"{kind}: staged copy refreshed")


@scenario("R37", "a committed .env directory is kept, not a crash (#37)")
def r37(ctx: Ctx) -> None:
    commit_with_index(ctx, "envdir", [("100644", ".env/config", "x=1\n")])
    run = ctx.tb("create", "--checkout", "envdir", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 0)
    warnings = (ctx.json_out(run) or {}).get("warnings", [])
    ctx.check(any(".env not copied" in w for w in warnings), f"--json warnings: {warnings}")
    path = wt(ctx, "envdir")
    ctx.check((path / ".env").is_dir(), ".env stays a directory")
    ctx.eq((path / ".env" / "config").read_text(), "x=1\n", "tracked content intact")
    ctx.eq(listed(ctx).get("envdir", {}).get("env"), "absent", "list env")
    enter = ctx.tb("enter", "envdir", *ctx.wt_args(), "--print")
    ctx.exit_is(enter, 0)
    ctx.check(".env not copied" in enter.stderr, "enter warns on stderr")
    ctx.check("Traceback" not in enter.stderr, "no traceback")


@scenario("R38", "a stray directory's hint leads to a working recovery (#38)")
def r38(ctx: Ctx) -> None:
    stray = wt(ctx, "junk")
    stray.mkdir(parents=True)
    run = ctx.tb("create", "junk", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 5)
    hint = ctx.json_err(run, "SLUG_CONFLICT").get("hint", "")
    ctx.check(
        "treebox teardown junk" not in hint, "hint does not recommend `treebox teardown junk`"
    )
    ctx.check("rm -rf" in hint, "hint gives the manual removal")
    ctx.exit_is(ctx.tb("teardown", "junk", *ctx.wt_args(), "--force"), 3)
    stray.rmdir()
    ctx.exit_is(ctx.tb("create", "junk", *ctx.wt_args(), "--print"), 0)


@scenario("R39", "cold dry-run plans match the cold setup (#39)")
def r39(ctx: Ctx) -> None:
    def plan(*extra: str) -> str:
        run = ctx.tb("create", "e2e-plan", *ctx.wt_args(), *extra, "--dry-run", "--json")
        ctx.exit_is(run, 0)
        return "\n".join((ctx.json_out(run) or {}).get("commands", []))

    def home_dirs() -> set[str]:
        # The default shared caches (uv, pnpm, npm, go, cargo) live here.
        caches = [".cache", ".local/share/pnpm", ".npm", "go", ".cargo"]
        found = {str(p) for c in caches for p in ctx.fx.home.glob(f"{c}/**") if p.is_dir()}
        return found | {c for c in caches if (ctx.fx.home / c).exists()}

    before = home_dirs()
    warm, cold = plan(), plan("--cold")
    ctx.check("treebox-cold-" in cold and "treebox-cold-" not in warm, "host cold plan")
    dwarm = plan("--isolation", "docker", "--template", OFFLINE_TEMPLATE)
    dcold = plan("--isolation", "docker", "--template", OFFLINE_TEMPLATE, "--cold")
    ctx.check("target=/caches/" in dwarm, "docker warm plan mounts the shared caches")
    ctx.check("target=/caches/" not in dcold, "docker cold plan omits them")
    ctx.eq(sorted(home_dirs() - before), [], "cache dirs created by the dry runs")


@scenario("R43", "nested roots: enter and teardown keep the resolved path (#43)")
def r43(ctx: Ctx) -> None:
    outer = ctx.sbx / "R"
    created(ctx, "foo", root=outer)
    ctx.git("branch", "nested-branch", "origin/main")
    created(ctx, "foo", "--checkout", "nested-branch", root=outer / "nested")
    enter = ctx.tb("enter", "nested-branch", *ctx.wt_args(outer), "--json")
    ctx.exit_is(enter, 0)
    ctx.eq((ctx.json_out(enter) or {}).get("worktree_path"), str(outer / "nested" / "foo"), "path")
    td = ctx.tb("teardown", "nested-branch", *ctx.wt_args(outer), "--force", "--json")
    ctx.exit_is(td, 0)
    ctx.check(not (outer / "nested" / "foo").exists(), "the requested worktree removed")
    ctx.check((outer / "foo").is_dir(), "the same-leaf sibling untouched")


def add_submodule(ctx: Ctx) -> None:
    sub_origin = ctx.sbx / "sub.git"
    subwork = ctx.sbx / "subwork"
    ctx.sh("git", "init", "-q", "--bare", "-b", "main", str(sub_origin))
    ctx.sh("git", "init", "-q", "-b", "main", str(subwork))
    (subwork / "lib.txt").write_text("hello from submodule\n")
    ctx.git("add", "-A", cwd=subwork)
    ctx.git("commit", "-qm", "sub", cwd=subwork)
    ctx.git("push", "-q", str(sub_origin), "main", cwd=subwork)
    ctx.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "sub")
    ctx.git("commit", "-qm", "add submodule")
    ctx.git("push", "-q", "origin", "main")


@scenario("R45", "submodule copy never writes through a planted link (#45)")
def r45(ctx: Ctx) -> None:
    add_submodule(ctx)
    outside = ctx.sbx / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("host data\n")
    (ctx.repo / "sub" / "inner").mkdir()
    (ctx.repo / "sub" / "inner" / "victim").write_text("source data\n")
    # The branch replaces the submodule entry with a tree whose "inner" is a
    # symlink to a directory outside the worktree.
    commit_with_index(
        ctx, "copy-target", [("remove", "sub", ""), ("120000", "sub/inner", str(outside))]
    )
    run = ctx.tb("create", "--checkout", "copy-target", *ctx.wt_args(), "--json")
    ctx.exit_is(run, 5)
    err = ctx.json_err(run, "DESTINATION_CONFLICT")
    link = wt(ctx, "copy-target") / "sub" / "inner"
    ctx.eq(err.get("path"), str(link), "error.path")
    ctx.eq(victim.read_text(), "host data\n", "outside file untouched")
    link.unlink()
    retry = ctx.tb("create", "--checkout", "copy-target", *ctx.wt_args(), "--json")
    ctx.exit_is(retry, 0)
    ctx.eq((link / "victim").read_text(), "source data\n", "submodule copied after the fix")
    ctx.eq(victim.read_text(), "host data\n", "outside file still untouched")


@scenario("R45b", "an unreadable submodule source is SUBMODULE_COPY_FAILED", requires=("non-root",))
def r45b(ctx: Ctx) -> None:
    add_submodule(ctx)
    secret = ctx.repo / "sub" / "unreadable.txt"
    secret.write_text("no read\n")
    secret.chmod(0o000)
    try:
        run = ctx.tb("create", "r45b", *ctx.wt_args(), "--json")
        ctx.exit_is(run, 1)
        ctx.json_err(run, "SUBMODULE_COPY_FAILED")
        ctx.check("Traceback" not in run.stderr, "no traceback")
    finally:
        secret.chmod(0o644)
        secret.unlink()


@scenario("R46", "merged badge only for the merged tip (#46)", evidence="boundary")
def r46(ctx: Ctx) -> None:
    db = ctx.sbx / "fake-gh"
    db.mkdir()
    created(ctx, "reused")
    created(ctx, "squashed")
    for name in ("reused", "squashed"):
        path = wt(ctx, name)
        (path / f"{name}.txt").write_text(f"{name}\n")
        ctx.git("add", "-A", cwd=path)
        ctx.git("commit", "-qm", f"{name} work", cwd=path)
        ctx.git("push", "-q", "-u", "origin", name, cwd=path)
        tip = ctx.git("rev-parse", "HEAD", cwd=path)
        pr = {"state": "MERGED", "number": 40 + len(name), "url": "", "isDraft": False}
        (db / name).write_text(json.dumps({**pr, "headRefOid": tip}))
    reused = wt(ctx, "reused")
    (reused / "more.txt").write_text("new work after the merge\n")
    ctx.git("add", "-A", cwd=reused)
    ctx.git("commit", "-qm", "new work", cwd=reused)
    ctx.git("push", "-q", "origin", "reused", cwd=reused)
    # The forge is attributed by the origin host. ".invalid" never resolves
    # (RFC 2606), so the chooser's best-effort fetch fails fast and offline.
    ctx.git("remote", "set-url", "origin", "https://forge.invalid/e2e/demo.git")
    env = dict(ctx.env, TREEBOX_E2E_FAKE_GH_DIR=str(db))
    env["PATH"] = os.pathsep.join([str(ctx.fx.stub_bin), str(FAKES_DIR), ctx.env["PATH"]])
    try:
        with ctx.pty("teardown", *ctx.wt_args(), env=env, cols=160) as term:
            ctx.check(term.expect(r"Select worktrees to tear down", timeout=60), "chooser shown")
            time.sleep(0.5)
            term.expect(r"$", timeout=1)
            text = term.screen_text()
            term.send("\x03")
            term.wait()
    finally:
        ctx.git("remote", "set-url", "origin", str(ctx.fx.origin))
    rows = {
        name: next((ln for ln in text.splitlines() if f" {name} " in f" {ln} "), "")
        for name in ("reused", "squashed")
    }
    ctx.check(
        "merge not verified for current tip" in rows["reused"],
        f"reused row: {rows['reused'].strip()!r}",
    )
    ctx.check("✓ merged" not in rows["reused"], "reused is not marked merged")
    ctx.check(
        "✓ merged (PR #48)" in rows["squashed"], f"squashed row: {rows['squashed'].strip()!r}"
    )
    ctx.check(wt(ctx, "reused").is_dir() and wt(ctx, "squashed").is_dir(), "nothing removed")


# --- U. unattended mode ------------------------------------------------------------------------


@group("U", "unattended mode")
def _setup_u(ctx: Ctx) -> None:
    created(ctx, "u1")


@scenario("U1", "create NAME -- ARGS puts the agent args in the entry command")
def u1(ctx: Ctx) -> None:
    run = ctx.tb("create", "u1-task", *ctx.wt_args(), "--json", "--", "-p", "a task")
    ctx.exit_is(run, 0)
    d = ctx.json_out(run) or {}
    entry = d.get("entry_command") or [""]
    ctx.check(bool(entry) and entry[-1].endswith("-p 'a task'"), f"entry_command: {entry[-1:]}")
    ctx.eq(d.get("warnings"), [], "warnings")
    replay = ctx.sh(*entry, check=False)
    ctx.eq(
        (stub_record(replay.stdout) or {}).get("args"),
        [
            "--dangerously-skip-permissions",
            "-p",
            "a task",
        ],
        "the replayed entry command gives the agent the args",
    )
    nameless = ctx.tb("create", *ctx.wt_args(), "--json", "--", "-p", "x")
    ctx.exit_is(nameless, 2)


@scenario("U2", "--non-interactive never prompts, also with a terminal", requires=("pty",))
def u2(ctx: Ctx) -> None:
    for args, env in (
        (["--non-interactive", "teardown", "u1"], ctx.env),
        (["teardown", "u1"], dict(ctx.env, TREEBOX_NON_INTERACTIVE="1")),
        (["--non-interactive", "teardown"], ctx.env),
    ):
        with ctx.pty(*args, *ctx.wt_args(), env=env) as term:
            code = term.wait(timeout=30)
        text = term.screen_text()
        ctx.eq(code, 5, f"exit of {' '.join(args)} on a terminal")
        ctx.check("Remove worktree" not in text and "Select worktrees" not in text, "no prompt")
        ctx.check("non-interactively" in text, "says it refuses without a prompt")
    ctx.check(wt(ctx, "u1").is_dir(), "nothing removed")


@scenario("U3", "the agent exit line names a non-zero agent status")
def u3(ctx: Ctx) -> None:
    for stub_arg, status, line in (
        ("--stub-exit=3", 3, "treebox: agent exited with status 3"),
        ("--stub-kill=KILL", 137, "treebox: agent exited with status 137 (killed by SIGKILL)"),
    ):
        run = ctx.tb("enter", "u1", *ctx.wt_args(), "--", stub_arg, tags=("agent-exit",))
        ctx.exit_is(run, status)
        ctx.eq(run.stderr.splitlines()[-1:], [line], "last stderr line")
    ok = ctx.tb("enter", "u1", *ctx.wt_args(), tags=("agent-exit",))
    ctx.exit_is(ok, 0)
    ctx.check("agent exited" not in ok.stderr, "no line for an agent exit of 0")


@scenario("U4", "a cancel signal stops the agent; treebox exits 128 + signal")
def u4(ctx: Ctx) -> None:
    for signum, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
        bg = ctx.tb_background(
            "enter", "u1", *ctx.wt_args(), "--", "--stub-sleep=60", tags=("agent-exit",)
        )
        rec = wait_for_stub(ctx, bg)
        if not ctx.check(rec is not None, "the agent runs"):
            bg.signal(signal.SIGKILL)
            bg.wait()
            return
        assert rec is not None
        bg.signal(signum)
        run = bg.wait(timeout=60)
        ctx.exit_is(run, code)
        ctx.check(f"Cancelled by {signum.name}" in run.stderr, "says it was cancelled")
        ctx.check("agent exited with status" not in run.stderr, "no agent exit line")
        try:
            os.kill(int(rec["pid"]), 0)
            alive = True
        except ProcessLookupError:
            alive = False
        ctx.check(not alive, f"the agent process {rec['pid']} is gone")
    ctx.check(wt(ctx, "u1").is_dir(), "the worktree is kept")
