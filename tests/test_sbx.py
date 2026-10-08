"""Unit tests for the sbx runner, with the sbx CLI mocked at the seam."""

from __future__ import annotations

import json
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from treebox import git
from treebox.config import Config, load_config, validate_config
from treebox.harnesses import get_harness
from treebox.models import Worktree, flatten_branch
from treebox.runners import PreflightError, SbxRunner, get_runner
from treebox.runners.sbx import _sanitize


class _FakeSbx:
    """Canned sbx CLI: records argv, serves ``ls --json`` from ``sandboxes``."""

    def __init__(self, sandboxes: list[dict] | None = None, *, rm_rc: int = 0, ls_rc: int = 0):
        self.sandboxes = sandboxes or []
        self.rm_rc = rm_rc
        self.ls_rc = ls_rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        rc, out, err = 0, "", ""
        if args[:2] == ["ls", "--json"]:
            rc = self.ls_rc
            out = json.dumps({"sandboxes": self.sandboxes}) if rc == 0 else ""
            err = "" if rc == 0 else "daemon not running"
        elif args[0] == "rm":
            rc = self.rm_rc
            err = "" if rc == 0 else "boom"
        return subprocess.CompletedProcess(["sbx", *args], rc, out, err)


class _Reporter:
    def __init__(self) -> None:
        self.steps: list[list[str]] = []
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def step(self, label, detail, argv, *, cwd=None, env=None):
        self.steps.append(list(argv))
        return ""

    def note(self, label, detail=""):
        self.notes.append(f"{label}: {detail}")

    def ok(self, label, detail=""):
        return None

    def info(self, msg):
        self.infos.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    @contextmanager
    def task(self, label, detail="done"):
        yield


@pytest.fixture
def common(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    c = tmp_path / "gitcommon" / ".git"
    c.mkdir(parents=True)
    monkeypatch.setattr(git, "common_dir", lambda repo: c)
    return c


def _wt(tmp_path: Path, branch: str = "feature/x") -> Worktree:
    name = flatten_branch(branch)
    path = tmp_path / "root" / name
    path.mkdir(parents=True, exist_ok=True)
    return Worktree("/repo", name, branch, "main", path)


def _row(runner: SbxRunner, wt: Worktree, *, status: str = "running") -> dict:
    return {"name": runner._slug(wt), "status": status, "workspaces": [str(wt.path)]}


# --- naming ----------------------------------------------------------------------


def test_slug_is_valid_deterministic_and_collision_safe(tmp_path: Path):
    runner = SbxRunner(Config(isolation="sbx"))
    a = runner._slug(_wt(tmp_path, "feature/Auth_v2"))
    assert a == runner._slug(_wt(tmp_path, "feature/Auth_v2"))
    # sbx: letters, digits, hyphens, periods; starts alphanumeric.
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", a)
    # Same name under a different root (another repo) must not collide.
    other = Worktree("/other", "feature--Auth_v2", "feature/Auth_v2", "main", tmp_path / "x" / "w")
    assert runner._slug(other) != a
    assert _sanitize("///") == "worktree"


# --- create argv -----------------------------------------------------------------


def test_create_argv_mounts_worktree_common_dir_and_readonly_hooks(tmp_path: Path, common: Path):
    runner = SbxRunner(Config(isolation="sbx", sbx_cpus=4, sbx_memory="8g", harness="codex"))
    wt = _wt(tmp_path)
    assert runner._create_command(wt) == [
        "sbx",
        "create",
        "--name",
        runner._slug(wt),
        "--cpus",
        "4",
        "--memory",
        "8g",
        "codex",
        str(wt.path),
        str(common),
        f"{common / 'hooks'}:ro",
    ]


def test_create_argv_omits_unset_resources(tmp_path: Path, common: Path):
    runner = SbxRunner(Config(isolation="sbx"))
    argv = runner._create_command(_wt(tmp_path))
    assert "--cpus" not in argv and "--memory" not in argv
    assert argv[argv.index("claude") + 1] == str(_wt(tmp_path).path)


def test_colon_in_path_is_a_loud_error(tmp_path: Path, common: Path):
    runner = SbxRunner(Config(isolation="sbx"))
    wt = _wt(tmp_path, "a:b")
    with pytest.raises(RuntimeError, match="containing ':'"):
        runner._create_command(wt)


def test_dry_run_setup_is_the_create_command(tmp_path: Path, common: Path):
    runner = SbxRunner(Config(isolation="sbx"))
    wt = _wt(tmp_path)
    (cmd,) = runner.dry_run_setup(wt)
    assert cmd.startswith(f"sbx create --name {runner._slug(wt)} claude ")


# --- setup -----------------------------------------------------------------------


def test_setup_creates_sandbox_and_hooks_dir(tmp_path: Path, common: Path):
    fake = _FakeSbx()
    runner = SbxRunner(Config(isolation="sbx"), sbx=fake)
    rep = _Reporter()
    runner.setup(_wt(tmp_path), cold=False, reporter=rep)
    assert [s[:2] for s in rep.steps] == [["sbx", "create"]]
    assert (common / "hooks").is_dir()


def test_setup_is_idempotent_for_own_sandbox(tmp_path: Path, common: Path):
    wt = _wt(tmp_path)
    runner = SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx())
    fake = _FakeSbx([_row(runner, wt, status="stopped")])
    runner = SbxRunner(Config(isolation="sbx"), sbx=fake)
    rep = _Reporter()
    runner.setup(wt, cold=False, reporter=rep)
    assert rep.steps == []
    assert any("reusing" in n for n in rep.notes)


def test_setup_refuses_foreign_sandbox_of_same_name(tmp_path: Path, common: Path):
    wt = _wt(tmp_path)
    probe = SbxRunner(Config(isolation="sbx"))
    row = {"name": probe._slug(wt), "status": "running", "workspaces": ["/somewhere/else"]}
    runner = SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx([row]))
    with pytest.raises(RuntimeError, match="not created for"):
        runner.setup(wt, cold=False, reporter=_Reporter())


# --- entry -----------------------------------------------------------------------


def test_entry_command_and_args(tmp_path: Path):
    runner = SbxRunner(Config(isolation="sbx"))
    wt = _wt(tmp_path)
    harness = get_harness("claude")
    base = ["sbx", "run", "--name", runner._slug(wt)]
    assert runner.entry_command(wt, harness=harness, args=[]) == base
    assert runner.entry_command(wt, harness=harness, args=["-p", "hi"]) == [
        *base,
        "--",
        "-p",
        "hi",
    ]


def test_prepare_entry_requires_existing_sandbox(tmp_path: Path):
    wt = _wt(tmp_path)
    runner = SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx())
    with pytest.raises(RuntimeError, match="No sandbox exists"):
        runner.prepare_entry(wt)


def test_prepare_entry_accepts_stopped_sandbox(tmp_path: Path):
    wt = _wt(tmp_path)
    probe = SbxRunner(Config(isolation="sbx"))
    fake = _FakeSbx([_row(probe, wt, status="stopped")])
    SbxRunner(Config(isolation="sbx"), sbx=fake).prepare_entry(wt)
    # `sbx run --name` restarts it; prepare_entry must not mutate anything.
    assert fake.calls == [["ls", "--json"]]


def test_refresh_is_a_noop(tmp_path: Path):
    fake = _FakeSbx()
    SbxRunner(Config(isolation="sbx"), sbx=fake).refresh(_wt(tmp_path), reporter=_Reporter())
    assert fake.calls == []


# --- preflight -------------------------------------------------------------------


def test_preflight_missing_sbx(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("treebox.runners.sbx.system.have", lambda cmd: False)
    monkeypatch.setattr("treebox.runners.sbx.platform.machine", lambda: "arm64")
    with pytest.raises(PreflightError) as exc:
        SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx()).preflight(_Reporter())
    assert "brew install docker/tap/sbx" in (exc.value.hint or "")


def test_preflight_unusable_sbx(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("treebox.runners.sbx.system.have", lambda cmd: True)
    monkeypatch.setattr("treebox.runners.sbx.platform.machine", lambda: "arm64")
    with pytest.raises(PreflightError) as exc:
        SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx(ls_rc=1)).preflight(_Reporter())
    assert exc.value.error_code == "SBX_UNAVAILABLE"
    assert "sbx login" in (exc.value.hint or "")


def test_preflight_rejects_intel_mac(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("treebox.runners.sbx.sys.platform", "darwin")
    monkeypatch.setattr("treebox.runners.sbx.platform.machine", lambda: "x86_64")
    with pytest.raises(PreflightError) as exc:
        SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx()).preflight(_Reporter())
    assert exc.value.error_code == "UNSUPPORTED_PLATFORM"


def test_preflight_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("treebox.runners.sbx.system.have", lambda cmd: True)
    monkeypatch.setattr("treebox.runners.sbx.platform.machine", lambda: "arm64")
    SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx()).preflight(_Reporter())


def test_facts_do_not_require_host_login():
    assert SbxRunner(Config(isolation="sbx")).facts().login_required is False


# --- teardown --------------------------------------------------------------------


@pytest.fixture
def have_sbx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("treebox.runners.sbx.system.have", lambda cmd: True)


def test_teardown_removes_recorded_sandbox_noninteractively(tmp_path: Path, have_sbx):
    wt = _wt(tmp_path)
    probe = SbxRunner(Config(isolation="sbx"))
    fake = _FakeSbx([_row(probe, wt)])
    runner = SbxRunner(Config(isolation="sbx"), sbx=fake)
    runner.initialize(wt)
    result = runner.teardown(wt, reporter=_Reporter())
    assert result.container == "cleaned"
    assert ["rm", "--force", runner._slug(wt)] in fake.calls
    assert not runner._manifest_file(wt).parent.exists()


def test_teardown_without_manifest_removes_nothing(tmp_path: Path, have_sbx):
    wt = _wt(tmp_path)
    probe = SbxRunner(Config(isolation="sbx"))
    fake = _FakeSbx([_row(probe, wt)])
    rep = _Reporter()
    result = SbxRunner(Config(isolation="sbx"), sbx=fake).teardown(wt, reporter=rep)
    assert result.container == "skipped"
    assert fake.calls == []
    assert rep.warnings


def test_teardown_ignores_tampered_manifest(tmp_path: Path, have_sbx):
    wt = _wt(tmp_path)
    runner = SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx())
    runner.initialize(wt)
    path = runner._manifest_file(wt)
    data = json.loads(path.read_text())
    data["sandbox"] = "someone-elses-sandbox"
    path.write_text(json.dumps(data))
    fake = _FakeSbx([{"name": "someone-elses-sandbox", "workspaces": [str(wt.path)]}])
    runner = SbxRunner(Config(isolation="sbx"), sbx=fake)
    assert runner.teardown(wt, reporter=_Reporter()).container == "skipped"
    assert not any(c[0] == "rm" for c in fake.calls)


def test_teardown_missing_sandbox_is_clean(tmp_path: Path, have_sbx):
    wt = _wt(tmp_path)
    fake = _FakeSbx()
    runner = SbxRunner(Config(isolation="sbx"), sbx=fake)
    runner.initialize(wt)
    assert runner.teardown(wt, reporter=_Reporter()).container == "cleaned"
    assert not any(c[0] == "rm" for c in fake.calls)


def test_teardown_rm_failure_warns_and_reports_failed(tmp_path: Path, have_sbx):
    wt = _wt(tmp_path)
    probe = SbxRunner(Config(isolation="sbx"))
    fake = _FakeSbx([_row(probe, wt)], rm_rc=1)
    runner = SbxRunner(Config(isolation="sbx"), sbx=fake)
    runner.initialize(wt)
    rep = _Reporter()
    assert runner.teardown(wt, reporter=rep).container == "failed"
    assert any("boom" in w for w in rep.warnings)
    assert runner._manifest_file(wt).exists()  # kept so ownership survives


def test_teardown_refuses_sandbox_bound_elsewhere(tmp_path: Path, have_sbx):
    wt = _wt(tmp_path)
    probe = SbxRunner(Config(isolation="sbx"))
    row = {"name": probe._slug(wt), "workspaces": ["/not/this/worktree"]}
    fake = _FakeSbx([row])
    runner = SbxRunner(Config(isolation="sbx"), sbx=fake)
    runner.initialize(wt)
    assert runner.teardown(wt, reporter=_Reporter()).container == "failed"
    assert not any(c[0] == "rm" for c in fake.calls)


def test_teardown_list_failure_is_best_effort(tmp_path: Path, have_sbx):
    wt = _wt(tmp_path)
    runner = SbxRunner(Config(isolation="sbx"), sbx=_FakeSbx(ls_rc=1))
    runner.initialize(wt)
    assert runner.teardown(wt, reporter=_Reporter()).container == "failed"


# --- config & registry -----------------------------------------------------------


def test_registry_resolves_sbx():
    assert isinstance(get_runner(Config(isolation="sbx")), SbxRunner)


def test_firewall_rejected_for_sbx():
    with pytest.raises(ValueError, match="sbx policy"):
        validate_config(Config(isolation="sbx", firewall=True))
    validate_config(Config(isolation="docker", firewall=True))


def test_template_rejected_for_sbx():
    with pytest.raises(ValueError, match="--template"):
        validate_config(Config(isolation="sbx", template="custom"))


@pytest.mark.parametrize("memory", ["8g", "512m", "16GiB", "2G"])
def test_sbx_memory_accepts_sizes(memory: str):
    validate_config(Config(isolation="sbx", sbx_memory=memory))


@pytest.mark.parametrize("memory", ["", "lots", "0g", "-1g", "8"])
def test_sbx_memory_rejects_garbage(memory: str):
    with pytest.raises(ValueError, match="sbx_memory"):
        validate_config(Config(isolation="sbx", sbx_memory=memory))


def test_sbx_cpus_must_be_positive():
    with pytest.raises(ValueError, match="sbx_cpus"):
        validate_config(Config(isolation="sbx", sbx_cpus=0))


def test_load_config_reads_sbx_keys(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('isolation = "sbx"\nsbx_cpus = 6\nsbx_memory = "12g"\n')
    cfg = load_config(cfg_file)
    assert (cfg.isolation, cfg.sbx_cpus, cfg.sbx_memory) == ("sbx", 6, "12g")


@pytest.mark.parametrize("line", ['sbx_cpus = "4"', "sbx_cpus = true", "sbx_memory = 8"])
def test_load_config_rejects_wrong_types(tmp_path: Path, line: str):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(line + "\n")
    with pytest.raises(ValueError, match="must be"):
        load_config(cfg_file)


# --- --sbx-cpus / --sbx-memory (CLI) ---------------------------------------------


@pytest.fixture
def fake_sbx_cli(monkeypatch: pytest.MonkeyPatch) -> _FakeSbx:
    """A healthy sbx for CLI-level tests: no real binary or daemon needed."""
    fake = _FakeSbx()
    monkeypatch.setattr("treebox.runners.sbx._sbx", fake)
    monkeypatch.setattr("treebox.runners.sbx.system.have", lambda cmd: True)
    monkeypatch.setattr("treebox.runners.sbx.platform.machine", lambda: "arm64")
    return fake


def _cli(args: list[str]):
    from typer.testing import CliRunner

    from treebox.cli import app

    return CliRunner().invoke(app, args, catch_exceptions=False)


def _dry(repo: Path, tmp_path: Path, *extra: str):
    root = str(tmp_path / "wts")
    return _cli(["create", "sized", "--repo", str(repo), "--root", root, "--dry-run", *extra])


def test_flags_reach_the_sbx_create_command(
    repo: Path, tmp_path: Path, hermetic_config, fake_sbx_cli
):
    res = _dry(repo, tmp_path, "--isolation", "sbx", "--sbx-cpus", "6", "--sbx-memory", "12g")
    assert res.exit_code == 0, res.output
    assert "--cpus 6 --memory 12g" in res.stderr


def test_flags_override_config_values(
    repo: Path, tmp_path: Path, hermetic_config, fake_sbx_cli, monkeypatch: pytest.MonkeyPatch
):
    cfg = tmp_path / "sbx.toml"
    cfg.write_text('isolation = "sbx"\nsbx_cpus = 2\nsbx_memory = "4g"\n')
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    # Config alone applies...
    assert "--cpus 2 --memory 4g" in _dry(repo, tmp_path).stderr
    # ...and a flag wins per key, leaving the other from config.
    res = _dry(repo, tmp_path, "--sbx-cpus", "8")
    assert "--cpus 8 --memory 4g" in res.stderr


@pytest.mark.parametrize("isolation", [None, "host", "docker"])
def test_flags_on_other_isolation_are_a_usage_error(
    repo: Path, tmp_path: Path, hermetic_config, isolation: str | None
):
    extra = ["--isolation", isolation] if isolation else []
    res = _dry(repo, tmp_path, *extra, "--sbx-cpus", "4")
    assert res.exit_code == 2, res.output
    assert "only apply to --isolation sbx" in res.stderr
    assert not (tmp_path / "wts").exists()  # nothing was provisioned
    res = _dry(repo, tmp_path, *extra, "--sbx-memory", "8g", "--json")
    assert res.exit_code == 2
    assert json.loads(res.stderr)["error"]["code"] == "INVALID_CONFIG"


def test_invalid_flag_values_are_a_usage_error(
    repo: Path, tmp_path: Path, hermetic_config, fake_sbx_cli
):
    assert _dry(repo, tmp_path, "--isolation", "sbx", "--sbx-memory", "lots").exit_code == 2
    assert _dry(repo, tmp_path, "--isolation", "sbx", "--sbx-cpus", "0").exit_code == 2
    assert _dry(repo, tmp_path, "--isolation", "sbx", "--sbx-cpus", "x").exit_code == 2


def test_sbx_config_keys_are_inert_for_other_runners(tmp_path: Path):
    """A global sbx_cpus/sbx_memory in config must not affect (or break) host
    or docker isolation: only an explicit flag is rejected there."""
    from treebox.runners import DockerRunner, HostRunner

    for iso, cls in (("host", HostRunner), ("docker", DockerRunner)):
        cfg = Config(isolation=iso, sbx_cpus=4, sbx_memory="8g")
        validate_config(cfg)
        assert isinstance(get_runner(cfg), cls)


def test_enter_has_no_sbx_resource_flags(repo: Path, tmp_path: Path, hermetic_config):
    res = _cli(["enter", "x", "--repo", str(repo), "--sbx-cpus", "4"])
    assert res.exit_code == 2
