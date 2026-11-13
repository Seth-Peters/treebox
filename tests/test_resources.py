"""Per-worktree CPU, memory and process limits (issue #67).

Covers the generic value grammar, precedence (flag, then user config, then the
operator template), the raw ``runArgs`` interplay, validation before
provisioning, the host runner's refusal, the host-owned record, entry on an
existing sandbox, and restart/recreate.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox import record, resources
from treebox.cli import EXIT_CONFLICT, EXIT_ERROR, EXIT_USAGE, app
from treebox.config import Config, load_config
from treebox.models import Worktree
from treebox.resources import ResourceLimitError, ResourceLimits
from treebox.runners import docker as dr
from treebox.runners.docker import DockerRunner, _split_limit_args

cli_runner = CliRunner()


def _run(args: list[str]):
    return cli_runner.invoke(app, args, catch_exceptions=False)


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path / "wts")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch TREEBOX_HOME / TREEBOX_CONFIG (never the real ~/.treebox) and
    an operator template dir that later tests write ``container.json`` into."""
    tb_home = tmp_path / "tbhome"
    tb_home.mkdir()
    monkeypatch.setenv("TREEBOX_HOME", str(tb_home))
    cfg = tb_home / "config.toml"
    cfg.write_text('setup_hook = ["true"]\n')
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    tpl = tmp_path / "tpl"
    tpl.mkdir()
    for name in ("Dockerfile", "post-create.sh", "init-firewall.sh", "allowed-domains.sh"):
        (tpl / name).write_text("#\n")
    _template(tpl, {})
    monkeypatch.setenv("TREEBOX_TEMPLATE_DIR", str(tpl))
    return tb_home


def _template(tpl: Path, extra: dict[str, object]) -> None:
    config = {"build": {"dockerfile": "Dockerfile"}, "user": "agent", "postCreate": "true"}
    (tpl / "container.json").write_text(json.dumps({**config, **extra}))


def _config(home: Path, text: str) -> None:
    (home / "config.toml").write_text('setup_hook = ["true"]\n' + text)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """A healthy fake Docker engine with 4 CPUs and no containers."""
    calls: list[list[str]] = []

    def fake(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        out = "4\n" if args[:3] == ["info", "--format", "{{.NCPU}}"] else ""
        return subprocess.CompletedProcess(["docker", *args], 0, out, "")

    monkeypatch.setattr(dr.system, "have", lambda c: True)
    monkeypatch.setattr(dr, "_docker", fake)
    return calls


def _dry(repo: Path, root: str, *extra: str):
    return _run(
        [
            "create",
            "lim",
            "--repo",
            str(repo),
            "--root",
            root,
            "--isolation",
            "docker",
            "--dry-run",
            "--json",
            *extra,
        ]
    )


def _run_line(payload: dict[str, object]) -> str:
    commands = payload["commands"]
    assert isinstance(commands, list)
    return next(c for c in commands if c.startswith("docker run"))


# --- the value grammar ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("4g", "4g"),
        ("4GB", "4g"),
        ("4gib", "4g"),
        ("4096m", "4g"),
        ("1.5g", "1536m"),
        ("512m", "512m"),
        ("6m", "6m"),
        ("6291457", "6291457"),
    ],
)
def test_memory_uses_docker_syntax_and_is_canonical(raw: str, canonical: str):
    assert resources.parse_memory(raw) == canonical


@pytest.mark.parametrize("raw", ["lots", "4gi", "1.5.2g", "-1g", "", "5m", "1024", "0"])
def test_memory_rejects_what_docker_rejects(raw: str):
    with pytest.raises(ResourceLimitError):
        resources.parse_memory(raw)


@pytest.mark.parametrize(("raw", "value"), [("2", 2.0), ("1.5", 1.5), ("0.01", 0.01), (3, 3.0)])
def test_cpus_accepts_decimals(raw: object, value: float):
    assert resources.parse_cpus(raw) == value


@pytest.mark.parametrize("raw", ["0", "0.001", "-1", "x", "nan", "inf", "1.0000000001", True])
def test_cpus_rejects_bad_values(raw: object):
    with pytest.raises(ResourceLimitError):
        resources.parse_cpus(raw)


@pytest.mark.parametrize("raw", ["0", "-5", "1.5", "x", -1, 0, True])
def test_pids_must_be_a_positive_whole_number(raw: object):
    with pytest.raises(ResourceLimitError):
        resources.parse_pids(raw)


def test_limits_precedence_is_field_by_field():
    flag = ResourceLimits(memory="8g")
    config = ResourceLimits(memory="4g", cpus=2.0)
    template = ResourceLimits(cpus=1.0, pids=64)
    assert flag.over(config).over(template) == ResourceLimits(cpus=2.0, memory="8g", pids=64)


# --- validation before provisioning ------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        ["--memory", "lots"],
        ["--cpus", "0"],
        ["--pids-limit", "-5"],
        ["--cpus", "99"],  # more than the engine's 4 CPUs
        ["--memory", "1m"],  # below Docker's 6m minimum
    ],
)
def test_bad_limit_is_classified_and_provisions_nothing(
    repo: Path, root: str, home: Path, engine, flags: list[str]
):
    res = _run(
        [
            "create",
            "lim",
            "--repo",
            str(repo),
            "--root",
            root,
            "--isolation",
            "docker",
            "--json",
            *flags,
        ]
    )
    assert res.exit_code == EXIT_USAGE, res.output
    assert json.loads(res.stderr)["error"]["code"] == "INVALID_RESOURCE"
    assert not Path(root).exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "lim"], capture_output=True, text=True
    ).stdout
    assert branches == ""
    assert not any(call[0] in ("build", "run") for call in engine)


@pytest.mark.parametrize(
    "table",
    [
        'memory = "lots"',
        "cpus = 0",
        'pids = "512"',
        'swap = "1g"',
    ],
)
def test_bad_resources_table_is_invalid_config(repo: Path, root: str, home: Path, table: str):
    _config(home, f"[resources]\n{table}\n")
    res = _dry(repo, root)
    assert res.exit_code == EXIT_USAGE, res.output
    assert json.loads(res.stderr)["error"]["code"] == "INVALID_CONFIG"


def test_resources_must_be_a_table(home: Path):
    _config(home, 'resources = "4g"\n')
    with pytest.raises(ValueError, match="resources"):
        load_config()


def test_resources_table_loads(home: Path):
    _config(home, '[resources]\ncpus = 2\nmemory = "4g"\npids = 512\n')
    assert load_config().resources == ResourceLimits(cpus=2.0, memory="4g", pids=512)


# --- precedence and the template tier --------------------------------------------


def test_no_limits_by_default(repo: Path, root: str, home: Path, engine):
    res = _dry(repo, root)
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["resources"] == {"cpus": None, "memory": None, "pids": None}
    run = _run_line(payload)
    assert "--cpus" not in run and "--memory" not in run and "--pids-limit" not in run


def test_flag_beats_config_beats_template(
    repo: Path, root: str, home: Path, tmp_path: Path, engine
):
    _template(tmp_path / "tpl", {"resources": {"cpus": 1, "memory": "1g", "pids": 64}})
    assert json.loads(_dry(repo, root).stdout)["resources"] == {
        "cpus": 1,
        "memory": "1g",
        "pids": 64,
    }
    _config(home, '[resources]\nmemory = "2g"\npids = 128\n')
    assert json.loads(_dry(repo, root).stdout)["resources"] == {
        "cpus": 1,
        "memory": "2g",
        "pids": 128,
    }
    payload = json.loads(_dry(repo, root, "--cpus", "1.5", "--memory", "3g").stdout)
    assert payload["resources"] == {"cpus": 1.5, "memory": "3g", "pids": 128}
    assert "--cpus 1.5 --memory 3g --pids-limit 128" in _run_line(payload)


def test_raw_runargs_limit_is_the_template_value(
    repo: Path, root: str, home: Path, tmp_path: Path, engine
):
    """A runArgs limit keeps working and is recorded, as the template tier."""
    _template(tmp_path / "tpl", {"runArgs": ["--cpus=2", "--memory", "4g", "--pids-limit=512"]})
    payload = json.loads(_dry(repo, root).stdout)
    assert payload["resources"] == {"cpus": 2, "memory": "4g", "pids": 512}
    run = _run_line(payload)
    assert run.count("--memory") == 1 and run.count("--cpus") == 1
    assert "--cpus 2 --memory 4g --pids-limit 512" in run


def test_treebox_limit_replaces_the_raw_runargs_flag(
    repo: Path, root: str, home: Path, tmp_path: Path, engine
):
    """Both present: the treebox value is the one docker gets, never Docker's
    last-flag-wins. Other runArgs stay."""
    _template(tmp_path / "tpl", {"runArgs": ["--shm-size=1g", "-m", "4g", "--cpus=2"]})
    payload = json.loads(_dry(repo, root, "--memory", "1g").stdout)
    assert payload["resources"] == {"cpus": 2, "memory": "1g", "pids": None}
    run = _run_line(payload)
    assert "4g" not in run
    assert run.count("--memory") == 1 and " -m " not in run
    assert "--shm-size=1g" in run


def test_template_with_two_values_for_one_limit_is_invalid(
    repo: Path, root: str, home: Path, tmp_path: Path, engine
):
    _template(tmp_path / "tpl", {"resources": {"memory": "2g"}, "runArgs": ["--memory=4g"]})
    res = _dry(repo, root)
    assert res.exit_code == EXIT_ERROR, res.output
    error = json.loads(res.stderr)["error"]
    assert error["code"] == "TEMPLATE_INVALID"
    assert "2g, 4g" in error["message"]


def test_template_resources_are_validated(
    repo: Path, root: str, home: Path, tmp_path: Path, engine
):
    _template(tmp_path / "tpl", {"resources": {"memory": "lots"}})
    res = _dry(repo, root)
    assert json.loads(res.stderr)["error"]["code"] == "TEMPLATE_INVALID"


@pytest.mark.parametrize(
    ("args", "kept", "found"),
    [
        (["--memory=4g", "--cap-add=X"], ["--cap-add=X"], {"memory": ["4g"]}),
        (["--memory", "4g"], [], {"memory": ["4g"]}),
        (["-m", "4g"], [], {"memory": ["4g"]}),
        (["-m4g"], [], {"memory": ["4g"]}),
        (["--memory-swap=8g"], ["--memory-swap=8g"], {}),
        (["--cpus", "2", "--pids-limit=9"], [], {"cpus": ["2"], "pids": ["9"]}),
    ],
)
def test_split_limit_args_spellings(args: list[str], kept: list[str], found: dict[str, list[str]]):
    remaining, raw = _split_limit_args(args, drop=["cpus", "memory", "pids"])
    assert remaining == kept
    assert {k: v for k, v in raw.items() if v} == found
    # Nothing set by treebox: the raw args pass through untouched.
    assert _split_limit_args(args)[0] == args


def test_no_resource_policy_is_read_from_the_target_repo(repo: Path, root: str, home: Path, engine):
    (repo / "config.toml").write_text('[resources]\nmemory = "1g"\n')
    (repo / ".treebox").mkdir(exist_ok=True)
    (repo / ".treebox" / "config.toml").write_text('[resources]\nmemory = "1g"\n')
    (repo / "container.json").write_text(json.dumps({"resources": {"memory": "1g"}}))
    payload = json.loads(_dry(repo, root).stdout)
    assert payload["resources"] == {"cpus": None, "memory": None, "pids": None}


# --- the host runner -----------------------------------------------------------


def test_host_refuses_an_explicit_limit(repo: Path, root: str, home: Path):
    res = _run(
        [
            "create",
            "lim",
            "--repo",
            str(repo),
            "--root",
            root,
            "--isolation",
            "host",
            "--memory",
            "4g",
            "--json",
        ]
    )
    assert res.exit_code == EXIT_USAGE, res.output
    assert json.loads(res.stderr)["error"]["code"] == "RESOURCE_UNSUPPORTED"
    assert not Path(root).exists()


def test_host_does_not_apply_config_limits(repo: Path, root: str, home: Path):
    """One [resources] table must not make every host worktree fail: the host
    applies no limit, records none, and says so."""
    _config(home, '[resources]\nmemory = "4g"\n')
    res = _run(["create", "lim", "--repo", str(repo), "--root", root, "--print"])
    assert res.exit_code == 0, res.output
    assert "[resources] memory not applied: the host runner" in res.stderr
    assert record.load(Path(root) / "lim").resources == ResourceLimits()
    listed = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
    assert listed["worktrees"][0]["resources"] == {"cpus": None, "memory": None, "pids": None}

    res = _run(["enter", "lim", "--repo", str(repo), "--root", root, "--print"])
    assert res.exit_code == 0 and "!" not in res.stderr
    res = _run(["enter", "lim", "--repo", str(repo), "--root", root, "--cpus", "2", "--json"])
    assert res.exit_code == EXIT_USAGE
    assert json.loads(res.stderr)["error"]["code"] == "RESOURCE_UNSUPPORTED"


# --- the record and entry -------------------------------------------------------


@pytest.mark.parametrize("key", ["resources", "explicitResources"])
def test_record_without_a_limit_key_is_invalid(tmp_path: Path, key: str):
    """``resources`` and ``explicitResources`` are required keys of the one
    record schema version: a record without one is refused, never read as
    "no treebox cap"."""
    wt = tmp_path / "wts" / "w"
    wt.mkdir(parents=True)
    record.save(record.WorktreeRecord(wt, "main", "docker", "claude", False, "default"))
    path = record.record_path(wt)
    data = json.loads(path.read_text())
    del data[key]
    path.write_text(json.dumps(data))
    with pytest.raises(record.RecordInvalidError, match=f"missing key\\(s\\) {key}"):
        record.load(wt)


def test_enter_refuses_a_record_without_resources_and_teardown_removes_it(
    repo: Path, root: str, home: Path
):
    _run(["create", "lim", "--repo", str(repo), "--root", root, "--print"])
    wt = Path(root) / "lim"
    path = record.record_path(wt)
    data = json.loads(path.read_text())
    del data["resources"]
    path.write_text(json.dumps(data))
    res = _enter(repo, root, "--json")
    assert res.exit_code == EXIT_CONFLICT, res.output
    error = json.loads(res.stderr)["error"]
    assert error["code"] == "RECORD_INVALID"
    assert "missing key(s) resources" in error["message"]
    assert "treebox teardown lim" in error["hint"]
    res = _run(["teardown", "lim", "--repo", str(repo), "--root", root, "--force", "--json"])
    assert res.exit_code == 0, res.output
    assert not wt.exists() and not path.exists()


def test_record_round_trips_limits(tmp_path: Path):
    wt = tmp_path / "wts" / "w"
    wt.mkdir(parents=True)
    limits = ResourceLimits(cpus=1.5, memory="4g", pids=512)
    rec = record.WorktreeRecord(
        wt, "main", "docker", "claude", False, "default", limits, frozenset({"pids", "cpus"})
    )
    record.save(rec)
    data = json.loads(record.record_path(wt).read_text())
    assert data["schemaVersion"] == record.SCHEMA_VERSION == 1
    assert data["resources"] == {"cpus": 1.5, "memory": "4g", "pids": 512}
    assert data["explicitResources"] == ["cpus", "pids"]  # display order
    assert record.load(wt) == rec


@pytest.mark.parametrize(
    ("explicit", "reason"),
    [
        (None, "missing key"),
        ("memory", "list of distinct limit names"),
        (["memory", "memory"], "list of distinct limit names"),
        ([1], "list of distinct limit names"),
        (["swap"], "unset limit"),
        (["cpus"], "unset limit"),  # cpus is not recorded: a flag always sets a value
    ],
)
def test_record_explicit_resources_are_validated(tmp_path: Path, explicit: object, reason: str):
    wt = tmp_path / "wts" / "w"
    wt.mkdir(parents=True)
    record.save(
        record.WorktreeRecord(
            wt, "main", "docker", "claude", False, "default", ResourceLimits(memory="4g")
        )
    )
    path = record.record_path(wt)
    data = json.loads(path.read_text())
    if explicit is None:
        del data["explicitResources"]
    else:
        data["explicitResources"] = explicit
    path.write_text(json.dumps(data))
    with pytest.raises(record.RecordInvalidError, match=reason):
        record.load(wt)


@pytest.fixture
def docker_cli(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real CLI on the docker runner with the fake engine: image build,
    setup, and container entry are stubbed (this is about the limit policy)."""
    monkeypatch.setattr(dr.DockerRunner, "setup", lambda self, w, cold, reporter: None)
    monkeypatch.setattr(dr.DockerRunner, "prepare_entry", lambda self, w: None)
    monkeypatch.setattr(dr.DockerRunner, "refresh", lambda self, w, reporter: None)


def _create(repo: Path, root: str, *extra: str):
    res = _run(
        ["create", "lim", "--repo", str(repo), "--root", root, "--isolation", "docker", *extra]
    )
    assert res.exit_code == 0, res.output
    return res


@pytest.fixture
def boxed(repo: Path, root: str, home: Path, docker_cli) -> Path:
    """A docker worktree created with the limits as explicit flags."""
    _create(repo, root, "--cpus", "1.5", "--memory", "4g", "--pids-limit", "512", "--print")
    return Path(root) / "lim"


def _enter(repo: Path, root: str, *extra: str):
    return _run(["enter", "lim", "--repo", str(repo), "--root", root, *extra])


_DRIFT = "limits: this sandbox keeps"
_NO_DRIFT = {"cpus": None, "memory": None, "pids": None}


def test_enter_with_a_different_limit_is_a_conflict_with_recreate_commands(
    repo: Path, root: str, boxed: Path
):
    res = _enter(repo, root, "--memory", "8g", "--json")
    assert res.exit_code == EXIT_CONFLICT, res.output
    error = json.loads(res.stderr)["error"]
    assert error["code"] == "RESOURCE_MISMATCH"
    assert error["hint"].endswith(
        "treebox teardown lim && treebox create lim --checkout lim --isolation docker "
        "--template default -H claude --cpus 1.5 --memory 8g --pids-limit 512"
    )
    # The record is unchanged: it is what the sandbox has.
    assert record.load(boxed).resources.memory == "4g"


def test_enter_restating_the_recorded_limit_is_fine(repo: Path, root: str, boxed: Path):
    res = _enter(repo, root, "--memory", "4096m", "--cpus", "1.5", "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["resources"] == {"cpus": 1.5, "memory": "4g", "pids": 512}


def test_explicit_flag_under_another_config_default_never_drifts(
    repo: Path, root: str, home: Path, docker_cli
):
    """`--memory 4g` under `memory = "2g"` is a choice, not drift: no warning
    on enter, also after the config default changes again."""
    _config(home, '[resources]\nmemory = "2g"\n')
    _create(repo, root, "--memory", "4g", "--print")
    assert record.load(Path(root) / "lim").explicit_resources == {"memory"}
    for text in ('[resources]\nmemory = "2g"\n', '[resources]\nmemory = "3g"\n'):
        _config(home, text)
        res = _enter(repo, root, "--print")
        assert res.exit_code == 0, res.output
        assert _DRIFT not in res.stderr
        res = _enter(repo, root, "--json")
        assert res.exit_code == 0, res.output
        assert json.loads(res.stdout)["resources_drift"] == _NO_DRIFT


def test_enter_warns_once_when_config_changed_after_create(
    repo: Path, root: str, home: Path, docker_cli
):
    _config(home, '[resources]\nmemory = "2g"\n')
    _create(repo, root, "--cpus", "1.5", "--print")
    res = _enter(repo, root, "--print")
    assert res.exit_code == 0 and _DRIFT not in res.stderr  # nothing changed yet
    _config(home, '[resources]\nmemory = "3g"\n')
    res = _enter(repo, root, "--print")
    assert res.exit_code == 0, res.output
    assert res.stderr.count(_DRIFT) == 1
    assert "keeps its recorded limits (cpus 1.5 · memory 2g)" in res.stderr
    assert "config and template now give memory 3g" in res.stderr
    # The re-create commands restate only the flag-chosen limit: memory comes
    # from the config again.
    assert "--template default -H claude --cpus 1.5\n" in res.stderr
    res = _enter(repo, root, "--json")
    assert res.exit_code == 0, res.output
    assert res.stderr == ""  # one JSON payload on stdout, nothing printed
    payload = json.loads(res.stdout)
    assert payload["resources_drift"] == {"cpus": None, "memory": "3g", "pids": None}
    assert payload["resources"] == {"cpus": 1.5, "memory": "2g", "pids": None}


def test_enter_warns_once_when_the_template_changed_after_create(
    repo: Path, root: str, home: Path, docker_cli, tmp_path: Path
):
    _template(tmp_path / "tpl", {"resources": {"pids": 256}})
    _create(repo, root, "--print")
    res = _enter(repo, root, "--print")
    assert res.exit_code == 0 and _DRIFT not in res.stderr
    _template(tmp_path / "tpl", {"resources": {"pids": 300}})
    res = _enter(repo, root, "--print")
    assert res.exit_code == 0, res.output
    assert res.stderr.count(_DRIFT) == 1
    assert "config and template now give pids 300" in res.stderr
    res = _enter(repo, root, "--json")
    assert json.loads(res.stdout)["resources_drift"] == {"cpus": None, "memory": None, "pids": 300}


def test_restating_the_recorded_value_is_silent(repo: Path, root: str, home: Path, docker_cli):
    _config(home, '[resources]\nmemory = "2g"\n')
    _create(repo, root, "--print")
    _config(home, '[resources]\nmemory = "3g"\n')
    res = _enter(repo, root, "--memory", "2048m", "--print")
    assert res.exit_code == 0, res.output
    assert _DRIFT not in res.stderr
    res = _enter(repo, root, "--memory", "2g", "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["resources_drift"] == _NO_DRIFT


def test_host_enter_has_no_drift(repo: Path, root: str, home: Path):
    _run(["create", "lim", "--repo", str(repo), "--root", root, "--print"])
    _config(home, '[resources]\nmemory = "3g"\n')
    res = _enter(repo, root, "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["resources_drift"] == _NO_DRIFT


def test_list_json_shows_recorded_limits(repo: Path, root: str, boxed: Path):
    listed = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
    assert listed["worktrees"][0]["resources"] == {"cpus": 1.5, "memory": "4g", "pids": 512}


def test_resumed_create_must_restate_the_recorded_limits(repo: Path, root: str, home: Path):
    from treebox import state

    _run(["create", "lim", "--repo", str(repo), "--root", root, "--print"])
    wt = Path(root) / "lim"
    record.save(dataclasses.replace(record.load(wt), resources=ResourceLimits(pids=64)))
    state.save(wt, state.WorktreeState(lockfile_hash="", provisioned=False))
    res = _run(["create", "lim", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == EXIT_CONFLICT, res.output
    error = json.loads(res.stderr)["error"]
    assert error["code"] == "RECORD_CONFLICT"
    assert "--pids-limit 64" in error["hint"]


def test_resume_restating_a_default_limit_by_flag_keeps_its_source(
    repo: Path, root: str, home: Path, docker_cli
):
    """The hint restates every recorded limit as a flag. That resume matches
    the record (the values are the policy), and the record keeps how each
    limit was first chosen, so a later config change still drifts."""
    from treebox import state

    _config(home, '[resources]\nmemory = "2g"\n')
    _create(repo, root, "--print")
    wt = Path(root) / "lim"
    state.save(wt, state.WorktreeState(lockfile_hash="", provisioned=False))
    _config(home, "")
    _create(repo, root, "--memory", "2g", "--print")
    assert record.load(wt).explicit_resources == frozenset()


# --- the docker run command: recorded limits on every (re)create -------------------


def _runner_command(tmp_path: Path, limits: ResourceLimits, run_args: list[str]) -> list[str]:
    runner = DockerRunner(Config(isolation="docker", resources=limits))
    wt_path = tmp_path / "root" / "w"
    wt_path.mkdir(parents=True, exist_ok=True)
    wt = Worktree("/repo", "w", "w", "main", wt_path)
    return runner._run_command(wt, {"runArgs": run_args})


def test_run_command_applies_recorded_limits(tmp_path: Path):
    """The run command comes from the record (``config.resources``), so a
    container re-created on enter gets the recorded limits, whatever the
    template says now; two worktrees get their own limits."""
    a = _runner_command(tmp_path, ResourceLimits(cpus=2.0, memory="4g", pids=512), ["--memory=1g"])
    b = _runner_command(tmp_path, ResourceLimits(memory="512m", pids=64), [])
    assert a[a.index("--cpus") + 1] == "2" and a[a.index("--memory") + 1] == "4g"
    assert "--memory=1g" not in a
    assert b[b.index("--memory") + 1] == "512m" and b[b.index("--pids-limit") + 1] == "64"
    assert "--cpus" not in b


def test_no_treebox_cap_keeps_raw_runargs(tmp_path: Path):
    """A worktree recorded with no treebox cap re-creates its container with
    the template's raw runArgs limits: they pass through unchanged."""
    argv = _runner_command(tmp_path, ResourceLimits(), ["--memory=4g", "--cpus=2"])
    assert "--memory=4g" in argv and "--cpus=2" in argv


def test_restart_does_not_recreate_the_container(tmp_path: Path, fake_common_dir_resources):
    """``prepare_entry`` restarts a stopped container in place (docker start),
    which keeps the HostConfig limits it was created with."""
    calls: list[list[str]] = []

    def fake(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        out = ""
        if args[0] == "ps":
            out = "abc\n"
        elif args[0] == "inspect":
            out = f"/{runner._slug(wt)}\nfalse\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    runner = DockerRunner(
        Config(isolation="docker", resources=ResourceLimits(memory="4g")), docker=fake
    )
    wt_path = tmp_path / "root" / "w"
    wt_path.mkdir(parents=True)
    wt = Worktree("/repo", "w", "w", "main", wt_path)
    runner.prepare_entry(wt)
    assert ["start", "abc"] in calls
    assert not any(c[0] == "run" for c in calls)


@pytest.fixture
def fake_common_dir_resources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from treebox import git

    common = tmp_path / "gitcommon" / ".git"
    common.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(git, "common_dir", lambda repo: common)
    return common


def test_runner_registry_declares_supported_limits():
    from treebox.runners import RUNNERS

    supported = {
        name: factory(Config(), False).supported_resources for name, factory in RUNNERS.items()
    }
    assert supported == {"host": frozenset(), "docker": frozenset(resources.RESOURCE_NAMES)}
