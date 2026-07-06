"""Contract-pinning tests for the CLI porcelain (issue #131, §1).

Agents script against three surfaces: the command/flag names, the ``--json``
payload shapes, and the exit-code / ``error.code`` matrix. These tests pin each
one *as a whole*, so an accidental rename, dropped field, or shifted exit code
fails an obvious test instead of silently breaking scripted callers.

A deliberate change to any surface must update the snapshot here — and, per the
porcelain discipline, removing/renaming a ``--json`` field means bumping
``SCHEMA_VERSION`` (fields are only ever *added* within a version).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from treebox import locking, state
from treebox.cli import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_NOTFOUND,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_USAGE,
    SCHEMA_VERSION,
    app,
)

cli_runner = CliRunner()


def _run(args: list[str]):
    return cli_runner.invoke(app, args, catch_exceptions=False)


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path / "wts")


# --- the numeric contract -------------------------------------------------------


def test_exit_code_constants_are_pinned():
    """The documented exit codes (--help epilog, docs/usage.md). Agents branch
    on the numbers; changing one is a breaking change to every scripted caller."""
    assert EXIT_OK == 0
    assert EXIT_ERROR == 1
    assert EXIT_USAGE == 2
    assert EXIT_NOTFOUND == 3
    assert EXIT_PERMISSION == 4
    assert EXIT_CONFLICT == 5


def test_schema_version_literal_is_pinned():
    """Every other test compares payloads against the imported SCHEMA_VERSION so
    a bump is a one-line change; THIS test pins the literal so that bump is a
    conscious, reviewed decision (it changes what agents receive)."""
    assert SCHEMA_VERSION == 1


# --- the command / flag surface --------------------------------------------------

# The full argument surface, straight from the click command tree. A rename, a
# dropped flag, a lost short alias, or a new required argument shows up here as
# a readable diff. New *optional* flags extend the sets below.
_COMMON_LIST = {"--repo", "--root", "--json"}
_VERBOSITY = {"--quiet", "-q", "--verbose", "-v"}
_TEARDOWN_OPTS = (
    _COMMON_LIST
    | _VERBOSITY
    | {
        "--isolation",
        "--delete-branch",
        "--remove-volumes",
        "--force",
        "--skip-container",
    }
)
EXPECTED_SURFACE: dict[str, tuple[set[str], list[str]]] = {
    # command: (option strings, positional argument names in order)
    "create": (
        _COMMON_LIST
        | _VERBOSITY
        | {
            "--checkout",
            "--base",
            "--isolation",
            "--harness",
            "-H",
            "--cold",
            "--no-fetch",
            "--firewall",
            "--no-firewall",
            "--template",
            "--dry-run",
            "-n",
            "--print",
        },
        ["name"],
    ),
    "enter": (
        _COMMON_LIST
        | _VERBOSITY
        | {"--isolation", "--harness", "-H", "--template", "--cold", "--print"},
        ["ref", "args"],
    ),
    "list": (_COMMON_LIST, []),
    "teardown": (_TEARDOWN_OPTS, ["refs"]),
    "doctor": ({"--repo", "--isolation", "--json"}, []),
    # `template` is a sub-app (group), so its own param surface is empty; its
    # subcommands are pinned in test_template_subcommand_surface_is_pinned.
    "template": (set(), []),
    "version": (set(), []),
    # Muscle-memory aliases: same callback, so same surface as their canonical
    # command (asserted structurally below, not re-listed).
    "ls": (_COMMON_LIST, []),
    "rm": (_TEARDOWN_OPTS, ["refs"]),
}


def _surface(command) -> tuple[set[str], list[str]]:
    opts: set[str] = set()
    args: list[str] = []
    for p in command.params:
        if p.param_type_name == "argument":
            args.append(p.name)
        else:
            opts.update(p.opts)
            opts.update(p.secondary_opts)
    return opts, args


def test_command_and_flag_surface_is_pinned():
    group = typer.main.get_command(app)
    assert sorted(group.commands) == sorted(EXPECTED_SURFACE)
    # ls / rm are hidden aliases; everything else is visible.
    assert {n for n, c in group.commands.items() if c.hidden} == {"ls", "rm"}
    for name, (want_opts, want_args) in EXPECTED_SURFACE.items():
        got_opts, got_args = _surface(group.commands[name])
        assert got_opts == want_opts, f"{name}: option surface changed"
        assert got_args == want_args, f"{name}: positional arguments changed"
    # The alias pairs must never drift from their canonical command.
    assert _surface(group.commands["ls"]) == _surface(group.commands["list"])
    assert _surface(group.commands["rm"]) == _surface(group.commands["teardown"])


def test_template_subcommand_surface_is_pinned():
    """The `template` sub-app's commands and flags are a stable surface too —
    scripts and docs depend on `template init/list/path` and their options."""
    group = typer.main.get_command(app)
    template = group.commands["template"]
    assert sorted(template.commands) == ["init", "list", "ls", "path"]
    assert _surface(template.commands["init"]) == ({"--from", "--force", "--json"}, ["name"])
    assert _surface(template.commands["list"]) == ({"--json"}, [])
    assert _surface(template.commands["path"]) == (set(), ["name"])
    # `ls` is a hidden alias of `list`, mirroring the top-level ls/rm pair — same
    # surface, never drifting from its canonical command.
    assert template.commands["ls"].hidden
    assert _surface(template.commands["ls"]) == _surface(template.commands["list"])


def test_app_level_version_flag():
    """`treebox --version` / `-V` (eager, exits before any command) and the
    `version` command must exist and agree."""
    group = typer.main.get_command(app)
    version_param = next(p for p in group.params if "--version" in p.opts)
    assert "-V" in version_param.opts

    by_flag = _run(["--version"])
    by_short = _run(["-V"])
    by_cmd = _run(["version"])
    assert by_flag.exit_code == by_short.exit_code == by_cmd.exit_code == 0
    assert by_flag.stdout == by_short.stdout == by_cmd.stdout
    assert by_flag.stdout.strip()  # a non-empty version string


# --- the --json payload shapes ----------------------------------------------------
# Key-set snapshots: existing tests assert individual keys, which catches a
# missing addition but not an accidental REMOVAL (a schema-breaking reshape).
# These pin the exact shape of every payload the CLI emits.

CREATE_KEYS = {
    "schemaVersion",
    "name",
    "worktree_path",
    "branch",
    "base",
    "entry_command",
    "created",
}
DRY_RUN_KEYS = {"schemaVersion", "dry_run", "name", "worktree_path", "branch", "commands"}
LIST_ROW_KEYS = {
    "name",
    "branch",
    "unnamed",
    "missing",
    "last_commit",
    "commit_epoch",
    "path",
    "base",
    "isolation",
    "harness",
    "deps",
    "env",
}
TEARDOWN_RECORD_KEYS = {
    "name",
    "branch",
    "worktree_path",
    "removed",
    "branch_deleted",
    "container",
    "volumes_removed",
}
CONTAINER_VALUES = {"cleaned", "skipped", "failed"}
DOCTOR_KEYS = {"schemaVersion", "ok", "isolation", "checks", "advisories"}
DOCTOR_CHECK_KEYS = {"name", "ok", "detail"}


def test_json_payload_shapes_are_pinned(repo: Path, root: str, hermetic_config):
    # dry-run
    payload = json.loads(
        _run(["create", "shape", "--repo", str(repo), "--root", root, "--dry-run", "--json"]).stdout
    )
    assert set(payload) == DRY_RUN_KEYS
    assert payload["schemaVersion"] == SCHEMA_VERSION
    assert isinstance(payload["commands"], list)

    # create / enter (same emitter: _emit_result)
    created = json.loads(
        _run(["create", "shape", "--repo", str(repo), "--root", root, "--json"]).stdout
    )
    assert set(created) == CREATE_KEYS
    assert isinstance(created["entry_command"], list)
    assert created["created"] is True
    entered = json.loads(
        _run(["enter", "shape", "--repo", str(repo), "--root", root, "--json"]).stdout
    )
    assert set(entered) == CREATE_KEYS
    assert entered["created"] is False

    # list
    listed = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
    assert set(listed) == {"schemaVersion", "worktrees"}
    (row,) = listed["worktrees"]
    assert set(row) == LIST_ROW_KEYS
    assert row["deps"] in {"fresh", "stale", "unknown"}
    assert row["env"] in {"present", "absent"}

    # teardown
    torn = json.loads(
        _run(["teardown", "shape", "--repo", str(repo), "--root", root, "--force", "--json"]).stdout
    )
    assert set(torn) == {"schemaVersion", "worktrees"}
    (record,) = torn["worktrees"]
    assert set(record) == TEARDOWN_RECORD_KEYS
    assert record["container"] in CONTAINER_VALUES

    # doctor
    doc = json.loads(_run(["doctor", "--repo", str(repo), "--json"]).stdout)
    assert set(doc) == DOCTOR_KEYS
    assert doc["isolation"] in {"host", "docker"}
    for check in doc["checks"]:
        assert set(check) == DOCTOR_CHECK_KEYS


def test_json_error_shape_is_pinned(repo: Path, root: str, hermetic_config):
    """The error object: code+message always; hint and path optional — and
    nothing else. All errors share one emitter (_die), so two probes suffice:
    one plain, one carrying both optional fields."""
    res = _run(["enter", "ghost", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == EXIT_NOTFOUND
    err = json.loads(res.stderr)
    assert set(err) == {"schemaVersion", "error"}
    assert err["schemaVersion"] == SCHEMA_VERSION
    assert {"code", "message"} <= set(err["error"]) <= {"code", "message", "hint", "path"}

    # DIRTY_WORKTREE carries the full optional set (hint + path).
    _run(["create", "dirty", "--repo", str(repo), "--root", root, "--print"])
    (Path(root) / "dirty" / "scratch.txt").write_text("x")
    res = _run(["teardown", "dirty", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == EXIT_CONFLICT
    err = json.loads(res.stderr)["error"]
    assert set(err) == {"code", "message", "hint", "path"}
    assert err["code"] == "DIRTY_WORKTREE"


# --- the exit-code / error.code matrix --------------------------------------------


def test_exit_and_error_code_matrix(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """One table for the whole scripting contract: scenario → (exit, error.code).
    Every case runs in --json mode so the code agents branch on is asserted too."""
    r = ["--repo", str(repo), "--root", root]
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    # Fixture setup for the conflict/ambiguity rows.
    assert _run(["create", "taken", *r, "--print"]).exit_code == 0
    (Path(root) / "taken" / "setup.log").unlink()  # keep it clean for NEEDS_CONFIRMATION
    assert _run(["create", "fix-auth", *r, "--print"]).exit_code == 0
    assert _run(["create", "fix-authz", *r, "--print"]).exit_code == 0
    (Path(root) / "fix-auth" / "dirty.txt").write_text("x")
    st = state.load(Path(root) / "fix-authz")
    assert st is not None
    st.isolation = "bogus"
    state.save(Path(root) / "fix-authz", st)
    subprocess.run(["git", "-C", str(repo), "branch", "spare"], check=True)

    matrix: list[tuple[list[str], int, str]] = [
        (["create", "Bad_Name", *r, "--json"], EXIT_USAGE, "INVALID_NAME"),
        (["create", "treebox/reserved", *r, "--json"], EXIT_USAGE, "INVALID_NAME"),
        (["create", "--checkout", "bad..ref", *r, "--json"], EXIT_USAGE, "INVALID_BRANCH"),
        (["list", "--repo", str(not_a_repo), "--json"], EXIT_USAGE, "NOT_A_REPO"),
        (["enter", "fix-", *r, "--json"], EXIT_USAGE, "AMBIGUOUS_REF"),
        (["enter", "ghost", *r, "--json"], EXIT_NOTFOUND, "NOT_FOUND"),
        (["create", "--checkout", "ghost/branch", *r, "--json"], EXIT_NOTFOUND, "NOT_FOUND"),
        (["create", "basey", "--base", "ghost", *r, "--json"], EXIT_NOTFOUND, "NOT_FOUND"),
        (["create", "taken", *r, "--json"], EXIT_CONFLICT, "SLUG_CONFLICT"),
        (["create", "spare", *r, "--json"], EXIT_CONFLICT, "BRANCH_EXISTS"),
        (["create", "dup", "--checkout", "main", *r, "--json"], EXIT_CONFLICT, "BRANCH_IN_USE"),
        (["teardown", "fix-auth", *r, "--json"], EXIT_CONFLICT, "DIRTY_WORKTREE"),
        (["teardown", "taken", *r, "--json"], EXIT_CONFLICT, "NEEDS_CONFIRMATION"),
        (["teardown", *r, "--json"], EXIT_CONFLICT, "NEEDS_CONFIRMATION"),
        (["enter", "fix-authz", *r, "--json"], EXIT_CONFLICT, "UNKNOWN_ISOLATION"),
        (
            ["enter", "taken", "--isolation", "docker", *r, "--json"],
            EXIT_CONFLICT,
            "ISOLATION_MISMATCH",
        ),
        (["create", "--isolation", "nope", *r, "--json"], EXIT_USAGE, "INVALID_CONFIG"),
    ]
    for args, want_exit, want_code in matrix:
        res = _run(args)
        assert res.exit_code == want_exit, (args, res.output, res.stderr)
        err = json.loads(res.stderr)
        assert err["error"]["code"] == want_code, args
        assert err["schemaVersion"] == SCHEMA_VERSION

    # LOCK_HELD (exit 5) needs the lock held around the invocation.
    with locking.worktree_lock(str(repo), root, "locked"):
        res = _run(["create", "locked", *r, "--json"])
    assert res.exit_code == EXIT_CONFLICT
    assert json.loads(res.stderr)["error"]["code"] == "LOCK_HELD"

    # FETCH_FAILED (exit 4): break origin, then restore it.
    origin_url = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "/nonexistent/x.git"], check=True
    )
    try:
        res = _run(["create", "fetchy", *r, "--json"])
        assert res.exit_code == EXIT_PERMISSION
        assert json.loads(res.stderr)["error"]["code"] == "FETCH_FAILED"
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "remote", "set-url", "origin", origin_url], check=True
        )

    # PreflightError keeps exit 1 with a stable error.code (agents branch on the
    # code, not the exit): docker binary present but daemon down, and missing.
    from treebox.runners import docker as dr

    monkeypatch.setattr(dr.system, "have", lambda c: True)
    monkeypatch.setattr(dr, "_docker_available", lambda: False)
    res = _run(["create", "boxed", "--isolation", "docker", *r, "--json"])
    assert res.exit_code == EXIT_ERROR
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "DOCKER_UNAVAILABLE" and "hint" in err

    monkeypatch.setattr(dr.system, "have", lambda c: False)
    res = _run(["create", "boxed", "--isolation", "docker", *r, "--json"])
    assert res.exit_code == EXIT_ERROR
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "MISSING_DEPENDENCY" and "hint" in err
