"""Unit tests for the pure logic: naming, hashing, config, docker sandbox config."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path

import pytest

from treebox import ecosystems
from treebox.config import Config, load_config
from treebox.models import Worktree, flatten_branch, worktree_path
from treebox.runners.docker import DockerRunner


def test_format_elapsed():
    from treebox.output import format_elapsed

    assert format_elapsed(0.008) == "8ms"
    assert format_elapsed(0.0) == "0ms"
    assert format_elapsed(0.999) == "999ms"
    assert format_elapsed(1.0) == "1.0s"
    assert format_elapsed(6.4) == "6.4s"


def test_spinner_and_status_glyphs_share_a_column():
    """The live spinner glyph must land in the same column as the ✓ it resolves
    into — a regression guard for the status-row / spinner alignment."""
    import io
    import re

    from rich.console import Console

    from treebox.output import _OK, THEME, Reporter

    r = Reporter()
    buf = io.StringIO()
    con = Console(file=buf, force_terminal=True, width=100, theme=THEME, highlight=False)
    con.print(r._spinner("container", style="wt.muted", indent=2))
    con.print(r._row(_OK, "wt.ok", "container", "wt.name", "ready", "wt.detail"))

    ansi = re.compile(r"\x1b\[[0-9;]*m")
    cols = []
    for line in buf.getvalue().splitlines():
        plain = ansi.sub("", line).rstrip()
        if plain:
            cols.append(len(plain) - len(plain.lstrip()))
    spinner_col, status_col = cols
    assert spinner_col == status_col == 2


def test_render_list_single_worktree_keeps_table_divider_without_summary():
    import io

    from rich.console import Console

    from treebox.output import THEME, Reporter

    buf = io.StringIO()
    reporter = Reporter()
    reporter.data_console = Console(
        file=buf,
        width=120,
        theme=THEME,
        highlight=False,
        color_system=None,
    )

    reporter.render_list(
        [
            {
                "name": "trusty-crane",
                "branch": "treebox/trusty-crane",
                "unnamed": True,
                "last_commit": "docs: clarify README wording",
                "commit_epoch": 0,
                "deps": "fresh",
                "env": "absent",
            }
        ],
        "/repo",
    )

    output = buf.getvalue()
    assert "NAME" in output
    assert "BRANCH" in output
    assert "LAST COMMIT" in output
    assert "trusty-crane" in output
    assert "─" in output
    assert "⚠ unnamed" in output
    assert "● fresh" in output
    assert "○ absent" in output
    assert "unnamed:" not in output
    assert "1 worktree" not in output


def test_render_list_multi_worktree_summary_is_separated():
    import io

    from rich.console import Console

    from treebox.output import THEME, Reporter

    buf = io.StringIO()
    reporter = Reporter()
    reporter.data_console = Console(
        file=buf,
        width=120,
        theme=THEME,
        highlight=False,
        color_system=None,
    )

    reporter.render_list(
        [
            {
                "name": "alpha",
                "branch": "treebox/alpha",
                "unnamed": True,
                "last_commit": "first",
                "commit_epoch": 1,
                "deps": "fresh",
                "env": "absent",
            },
            {
                "name": "beta",
                "branch": "feature/beta",
                "unnamed": False,
                "last_commit": "second",
                "commit_epoch": 1,
                "deps": "stale",
                "env": "present",
            },
        ],
        "/repo",
    )

    output = buf.getvalue()
    assert "beta" in output
    assert "\n\n  Summary: 2 worktrees" in output
    assert "1 unnamed" in output
    assert "1 stale" in output
    assert "Rename unnamed: git branch -m <type>/<short-name>" in output


def test_flatten_branch():
    assert flatten_branch("feature/auth") == "feature--auth"
    assert flatten_branch("bugfix/docs-outdated") == "bugfix--docs-outdated"
    assert flatten_branch("simple") == "simple"


def test_name_and_placeholder_helpers():
    from treebox.models import derive_name, is_placeholder, is_slug, placeholder_branch

    # NAME is one clean lowercase token — nothing else.
    assert is_slug("fix-auth") and is_slug("x2") and is_slug("brave-otter")
    assert not is_slug("Fix-Auth")
    assert not is_slug("feature/auth")
    assert not is_slug("two words")
    assert not is_slug("")

    assert placeholder_branch("brave-otter") == "treebox/brave-otter"
    assert is_placeholder("treebox/brave-otter")
    assert not is_placeholder("feature/auth") and not is_placeholder(None)

    # A placeholder implies its own slug; other branches flatten.
    assert derive_name("treebox/fix-auth") == "fix-auth"
    assert derive_name("feature/auth") == "feature--auth"


def test_is_valid_name_accepts_slash_separated_slugs():
    """An explicit create NAME doubles as the branch name: slug segments
    joined by slashes pass; anything else fails segment-wise via is_slug."""
    from treebox.models import is_valid_name

    assert is_valid_name("fix-auth")
    assert is_valid_name("feature/user-auth")
    assert is_valid_name("a/b/c2")
    assert not is_valid_name("Fix/Auth")
    assert not is_valid_name("two words")
    assert not is_valid_name("fix//x")
    assert not is_valid_name("/fix")
    assert not is_valid_name("fix/")
    assert not is_valid_name("")


def test_guard_detail_splits_by_placeholder():
    """The guard's reported consequence: rename-before-push for placeholders,
    a plain prefix statement for real branches (no bogus rename advice)."""
    from treebox.provision import _guard_detail

    assert "un-pushable until renamed" in _guard_detail("treebox/brave-otter")
    assert "treebox/brave-otter" in _guard_detail("treebox/brave-otter")
    assert _guard_detail("fix-login") == "treebox/* refs are un-pushable"


def test_petname_avoids_taken_names():
    import re

    from treebox.names import petname

    name = petname(lambda n: False)
    assert re.fullmatch(r"[a-z]+-[a-z]+", name)

    # A taken name is never returned.
    seen = {name}
    assert petname(lambda n: n in seen) not in seen

    # Every unnumbered combination taken → the numbered fallback, not a hang.
    fallback = petname(lambda n: not n[-1].isdigit())
    assert fallback.split("-")[-1].isdigit()


def test_format_age_is_compact():
    from treebox.output import format_age

    assert format_age(5) == "5s"
    assert format_age(300) == "5m"
    assert format_age(7200) == "2h"
    assert format_age(3 * 86400) == "3d"
    assert format_age(30 * 86400) == "4w"


def test_worktree_path_is_name_keyed(tmp_path: Path):
    # The name IS the directory leaf — no flattening at this layer.
    assert worktree_path("/r", ".wt", "fix-auth") == Path("/r/.wt/fix-auth")
    assert worktree_path("/r", "/abs/wt", "x") == Path("/abs/wt/x")


def test_worktree_path_expands_tilde_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert worktree_path("/r", "~/tilde-wts", "fix-auth") == home / "tilde-wts" / "fix-auth"


def test_unresolvable_tilde_root_stays_literal_repo_relative():
    # `Path.expanduser` would raise RuntimeError here; treebox must keep the
    # pre-expansion behavior: an unknown ~user marker is a literal name.
    assert worktree_path("/r", "~nosuchuser-treebox/wts", "x") == Path(
        "/r/~nosuchuser-treebox/wts/x"
    )


def test_resolve_ref_name_branch_substring(monkeypatch: pytest.MonkeyPatch):
    from treebox import git, resolve
    from treebox.provision import NotFoundError

    records = [
        git.WorktreeRecord("/r", "main"),  # the main checkout: outside the root
        git.WorktreeRecord("/r/.wt/fix-auth", "treebox/fix-auth"),
        git.WorktreeRecord("/r/.wt/fix-authz", "speed-up-ci"),
        git.WorktreeRecord("/r/.wt/detached", None),
    ]
    monkeypatch.setattr(git, "worktree_list", lambda repo: records)

    # Exact name wins even when it's also a substring of another candidate.
    assert resolve.resolve_ref("/r", ".wt", "fix-auth").name == "fix-auth"
    # Exact (live) branch.
    assert resolve.resolve_ref("/r", ".wt", "speed-up-ci").name == "fix-authz"
    # Unique substring of name or branch.
    assert resolve.resolve_ref("/r", ".wt", "up-ci").name == "fix-authz"
    assert resolve.resolve_ref("/r", ".wt", "detach").name == "detached"
    # Ambiguous substring names the matches; nothing is guessed.
    with pytest.raises(resolve.AmbiguousRefError, match="fix-auth"):
        resolve.resolve_ref("/r", ".wt", "fix-")
    # No match at all.
    with pytest.raises(NotFoundError, match="ghost"):
        resolve.resolve_ref("/r", ".wt", "ghost")
    # The main checkout never resolves: 'main' isn't under the worktree root.
    with pytest.raises(NotFoundError):
        resolve.resolve_ref("/r", ".wt", "main")


def test_resolve_ref_rejects_empty_ref(monkeypatch: pytest.MonkeyPatch):
    from treebox import git, resolve
    from treebox.provision import NotFoundError

    records = [
        git.WorktreeRecord("/r/.wt/only-one", "treebox/only-one"),
    ]
    monkeypatch.setattr(git, "worktree_list", lambda repo: records)

    # An empty ref (`"" in c.name` is always True) must not fall through to the
    # substring pass and resolve to the sole worktree — it is rejected up front.
    with pytest.raises(NotFoundError):
        resolve.resolve_ref("/r", ".wt", "")
    # Whitespace-only is treated the same way.
    with pytest.raises(NotFoundError):
        resolve.resolve_ref("/r", ".wt", "   ")


def test_worktree_discovery_canonicalizes_symlinked_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from treebox import ecosystems, git, resolve, state
    from treebox.cli import _collect_rows
    from treebox.config import Config

    repo = tmp_path / "repo"
    repo.mkdir()
    real_root = tmp_path / "real-wts"
    real_root.mkdir()
    alias_root = tmp_path / "alias-wts"
    try:
        alias_root.symlink_to(real_root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    wt = real_root / "feature-smoke"
    wt.mkdir()
    (wt / ".env").write_text("SECRET=canonical\n")
    records = [
        git.WorktreeRecord(str(repo), "main"),
        git.WorktreeRecord(str(wt), "treebox/feature-smoke"),
    ]
    monkeypatch.setattr(git, "worktree_list", lambda repo: records)
    monkeypatch.setattr(git, "last_commit", lambda path: ("init", 1))
    monkeypatch.setattr(ecosystems, "lockfile_hash", lambda path: "lock")
    monkeypatch.setattr(
        state,
        "load",
        lambda path: state.WorktreeState(
            base="main",
            isolation="host",
            harness="claude",
            lockfile_hash="lock",
            provisioned=True,
        ),
    )

    alias_wt = alias_root / "feature-smoke"
    assert git.branch_for_path(repo, str(alias_wt)) == "treebox/feature-smoke"
    assert git.worktree_registered(repo, str(alias_wt))
    assert resolve.resolve_ref(str(repo), str(alias_root), "feature-smoke").path == str(wt)

    rows = _collect_rows(str(repo), Config(root=str(alias_root)))
    assert [row["name"] for row in rows] == ["feature-smoke"]
    assert rows[0]["deps"] == "fresh"
    assert rows[0]["env"] == "present"


def test_https_remote_normalizes_github_urls():
    from treebox.git import _https_remote

    https = "https://github.com/o/r.git"
    assert _https_remote("git@github.com:o/r.git") == ("github.com", https)
    assert _https_remote("ssh://git@github.com/o/r.git") == ("github.com", https)
    assert _https_remote("https://github.com/o/r.git") == ("github.com", https)
    # Missing .git suffix is added; host is extracted from each shape.
    assert _https_remote("git@github.com:o/r") == ("github.com", https)
    assert _https_remote("git@example.org:team/proj") == (
        "example.org",
        "https://example.org/team/proj.git",
    )
    # Local paths and junk can't be rewritten to an HTTPS remote.
    assert _https_remote("/local/path/repo") is None
    assert _https_remote("") is None


def test_detect_pnpm_wins_over_npm(tmp_path: Path):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "package-lock.json").write_text("")
    names = {e.name for e in ecosystems.detect(tmp_path)}
    assert "pnpm" in names and "npm" not in names


def test_lockfile_hash_changes_with_content(tmp_path: Path):
    (tmp_path / "uv.lock").write_text("a")
    h1 = ecosystems.lockfile_hash(tmp_path)
    (tmp_path / "uv.lock").write_text("b")
    h2 = ecosystems.lockfile_hash(tmp_path)
    assert h1 and h2 and h1 != h2


def test_lockfile_hash_empty_without_manifests(tmp_path: Path):
    assert ecosystems.lockfile_hash(tmp_path) == ""


def test_setup_steps_wire_cache_env(tmp_path: Path):
    (tmp_path / "uv.lock").write_text("")
    caches = {"uv": str(tmp_path / "uvcache")}
    steps = ecosystems.setup_steps(ecosystems.detect(tmp_path), caches, cold_cache_root=None)
    uv = next(s for s in steps if s.name == "uv")
    assert uv.env["UV_CACHE_DIR"] == caches["uv"]
    assert uv.argv[:2] == ["uv", "sync"]


def test_setup_steps_expand_tilde_cache_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "uv.lock").write_text("")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[caches]\nuv = "~/tilde-uv-cache"\n')
    cfg = load_config(cfg_file)

    steps = ecosystems.setup_steps(ecosystems.detect(tmp_path), cfg.caches, cold_cache_root=None)
    uv = next(s for s in steps if s.name == "uv")
    assert uv.env["UV_CACHE_DIR"] == str(home / "tilde-uv-cache")
    assert ecosystems.cache_env(cfg.caches)["UV_CACHE_DIR"] == str(home / "tilde-uv-cache")
    assert (home / "tilde-uv-cache").is_dir()


def test_setup_steps_cold_redirects_cache(tmp_path: Path):
    (tmp_path / "uv.lock").write_text("")
    cold = str(tmp_path / "cold")
    steps = ecosystems.setup_steps(
        ecosystems.detect(tmp_path), {"uv": "/shared"}, cold_cache_root=cold
    )
    uv = next(s for s in steps if s.name == "uv")
    assert uv.env["UV_CACHE_DIR"].startswith(cold)


@pytest.mark.parametrize(
    ("lockfile", "name", "command", "env_var", "flag"),
    [
        ("uv.lock", "uv", ["uv", "sync"], "UV_CACHE_DIR", None),
        (
            "pnpm-lock.yaml",
            "pnpm",
            ["pnpm", "install", "--frozen-lockfile"],
            None,
            "--store-dir",
        ),
        ("package-lock.json", "npm", ["npm", "ci"], "npm_config_cache", None),
        ("go.sum", "go", ["go", "mod", "download"], "GOMODCACHE", None),
        ("Cargo.lock", "cargo", ["cargo", "fetch"], "CARGO_HOME", None),
    ],
)
def test_setup_steps_wire_each_ecosystem(
    tmp_path: Path,
    lockfile: str,
    name: str,
    command: list[str],
    env_var: str | None,
    flag: str | None,
):
    """Every ecosystem's exact setup argv and cache wiring: env-var-driven
    caches land in step.env; flag-driven caches (pnpm's --store-dir) ride on
    the argv instead."""
    (tmp_path / lockfile).write_text("")
    cache_dir = str(tmp_path / f"{name}-cache")
    steps = ecosystems.setup_steps(
        ecosystems.detect(tmp_path), {name: cache_dir}, cold_cache_root=None
    )
    (step,) = steps
    assert step.name == name
    if flag:
        assert step.argv == [*command, flag, cache_dir]
        assert step.env == {}
    else:
        assert step.argv == command
        assert step.env == {env_var: cache_dir}


def test_cache_dir_routing_agrees_between_setup_steps_and_cache_env(tmp_path: Path):
    """setup_steps and cache_env share one cold-vs-warm cache-dir resolution:
    an unset/empty configured cache is skipped identically by both, and --cold
    redirects both away from the shared store."""
    (tmp_path / "uv.lock").write_text("")
    detected = ecosystems.detect(tmp_path)
    for caches in ({}, {"uv": ""}):
        steps = ecosystems.setup_steps(detected, caches, cold_cache_root=None)
        uv = next(s for s in steps if s.name == "uv")
        assert "UV_CACHE_DIR" not in uv.env
        assert ecosystems.cache_env(caches) == {}
    cold = str(tmp_path / "cold")
    steps = ecosystems.setup_steps(detected, {}, cold_cache_root=cold)
    uv = next(s for s in steps if s.name == "uv")
    env = ecosystems.cache_env({}, cold_cache_root=cold)
    assert uv.env["UV_CACHE_DIR"] == env["UV_CACHE_DIR"] == str(Path(cold) / "uv")


def test_config_env_file_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from treebox.provision import resolve_env_file

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('env_file = "~/tilde-secrets/.env"\n')
    cfg = load_config(cfg_file)

    assert resolve_env_file(tmp_path / "repo", cfg.env_file) == home / "tilde-secrets" / ".env"


def test_config_unresolvable_tilde_user_stays_literal(tmp_path: Path):
    # A ~user marker that resolves to no account must load as a literal path,
    # not raise mid-load — config mistakes stay clean usage errors, never
    # tracebacks (see test_invalid_config_is_clean_usage_error).
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'root = "~nosuchuser-treebox/wts"\n'
        'env_file = "~nosuchuser-treebox/.env"\n'
        "[caches]\n"
        'uv = "~nosuchuser-treebox/cache"\n'
    )
    cfg = load_config(cfg_file)

    assert cfg.root == "~nosuchuser-treebox/wts"
    assert cfg.env_file == "~nosuchuser-treebox/.env"
    assert cfg.caches["uv"] == "~nosuchuser-treebox/cache"


def test_config_defaults_and_validation(tmp_path: Path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.isolation == "host" and cfg.harness == "claude"
    bad = tmp_path / "bad.toml"
    bad.write_text('isolation = "nope"\n')
    with pytest.raises(ValueError):
        load_config(bad)


def test_config_overrides_skip_none():
    cfg = Config()
    out = cfg.with_overrides(isolation="docker", harness=None)
    assert out.isolation == "docker" and out.harness == "claude"


# --- docker runner: config rendering -------------------------------------------


@pytest.fixture
def fake_common_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The docker runner mounts the repo's git common dir (and, on setup,
    materializes its ``hooks/``); unit tests use fake repo paths, so resolve it
    to a real temp dir without shelling out to git. Returns the dir so mount
    assertions can reference it."""
    from treebox import git

    common = tmp_path / "gitcommon" / ".git"
    common.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(git, "common_dir", lambda repo: common)
    return common


def _boxed_worktree(tmp_path: Path, branch: str = "feature/x") -> Worktree:
    name = flatten_branch(branch)
    wt_path = tmp_path / "root" / name
    wt_path.mkdir(parents=True, exist_ok=True)
    return Worktree("/repo", name, branch, "main", wt_path)


def test_docker_cache_mount_expands_tilde_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_common_dir
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    runner = DockerRunner(Config(isolation="docker", caches={"uv": "~/tilde-uv-cache"}))
    wt = _boxed_worktree(tmp_path)

    runner._write_config(wt, cold=False)

    data = json.loads(runner._config_file(wt).read_text())
    expected = "type=bind,source=" + str(home / "tilde-uv-cache") + ",target=/caches/uv"
    assert expected in data["mounts"]
    assert data["env"]["UV_CACHE_DIR"] == "/caches/uv"


def test_docker_config_injection(tmp_path: Path, fake_common_dir):
    cfg = Config(isolation="docker", firewall=True, caches={"uv": str(tmp_path / "uvc")})
    runner = DockerRunner(cfg)
    wt = _boxed_worktree(tmp_path)
    returned = runner._write_config(wt, cold=False)

    data = json.loads(runner._config_file(wt).read_text())
    # _write_config returns the exact config it wrote.
    assert returned == data
    # UID/GID pinned for correct ownership.
    assert data["build"]["args"]["USER_UID"]
    # Cache bind-mounted + env wired.
    assert f"type=bind,source={tmp_path / 'uvc'},target=/caches/uv" in data["mounts"]
    assert data["env"]["UV_CACHE_DIR"] == "/caches/uv"
    # Scoped credential copies bind-mounted — never the live host login dirs.
    creds = runner._creds_dir(wt)
    assert f"type=bind,source={creds / 'claude'},target=/home/agent/.claude" in data["mounts"]
    assert f"type=bind,source={creds / 'codex'},target=/home/agent/.codex" in data["mounts"]
    assert not any(f"source={Path.home()}/.claude" in m for m in data["mounts"])
    assert not any(f"source={Path.home()}/.codex" in m for m in data["mounts"])
    # The worktree and its git common dir are mounted at their host paths, so
    # in-container git resolves the worktree's gitdir pointers unchanged.
    assert f"type=bind,source={wt.path},target={wt.path}" in data["mounts"]
    assert f"type=bind,source={fake_common_dir},target={fake_common_dir}" in data["mounts"]
    # The shared hooks/ are executed by host git; mount them read-only so a
    # boxed agent can't plant a host-run hook (the common dir stays writable).
    hooks = fake_common_dir / "hooks"
    assert f"type=bind,source={hooks},target={hooks},readonly" in data["mounts"]
    # Firewall overlay merged in.
    assert "--cap-add=NET_ADMIN" in data["runArgs"]
    assert data["env"]["TREEBOX_FIREWALL"] == "1"


def test_docker_mounts_every_ecosystem_cache(tmp_path: Path, fake_common_dir):
    """Regression for the pnpm drift: cache mounts are driven by ECOSYSTEMS
    (the single source of cache wiring), not a hand-maintained second table
    that pnpm was missing from — every configured cache gets a bind mount and
    an env var pointing the in-container tool at it."""
    caches = {
        eco.cache_key: str(tmp_path / eco.name) for eco in ecosystems.ECOSYSTEMS if eco.cache_key
    }
    runner = DockerRunner(Config(isolation="docker", caches=caches))
    wt = _boxed_worktree(tmp_path)
    runner._write_config(wt, cold=False)
    data = json.loads(runner._config_file(wt).read_text())

    for eco in ecosystems.ECOSYSTEMS:
        target = eco.container_cache_target()
        assert target and any(f"target={target}" in m for m in data["mounts"])
    # pnpm takes --store-dir on the host; in-container it reads the npm-style
    # env form, so the mounted store must be wired via npm_config_store_dir.
    assert data["env"]["npm_config_store_dir"] == "/caches/pnpm"


def test_default_caches_cover_every_ecosystem():
    """config.default_caches is derived from ECOSYSTEMS, so every cached
    ecosystem has a default shared host store (no third table to drift)."""
    from treebox.config import default_caches

    keys = {eco.cache_key for eco in ecosystems.ECOSYSTEMS if eco.cache_key}
    assert set(default_caches()) == keys
    assert all(default_caches().values())


def test_docker_config_is_outside_the_worktree(tmp_path: Path, fake_common_dir):
    """Lockdown invariant: the sandbox-defining config is NOT in the mounted
    worktree, so a boxed agent can neither see nor edit it."""
    runner = DockerRunner(Config(isolation="docker"))
    wt = _boxed_worktree(tmp_path)
    runner._write_config(wt, cold=True)

    # Nothing written inside the worktree (the only repo tree mounted into the box).
    assert list(wt.path.iterdir()) == []
    # The config the container actually uses lives outside the worktree subtree.
    cfg_file = runner._config_file(wt)
    assert cfg_file.is_file()
    assert wt.path not in cfg_file.parents
    # The build context excludes the staged credential copies.
    assert "credentials/" in (runner._config_dir(wt) / ".dockerignore").read_text()


def test_docker_regenerates_config_each_run(tmp_path: Path, fake_common_dir):
    """Operator template is the single source of truth: a tampered config dir is
    wiped and re-rendered, so in-tree edits by the agent are inert."""
    runner = DockerRunner(Config(isolation="docker"))
    wt = _boxed_worktree(tmp_path)
    runner._write_config(wt, cold=True)
    planted = runner._config_dir(wt) / "EVIL"
    planted.write_text("pwn")
    # The credentials subdir must survive regeneration: a live container's bind
    # mounts point at it, and recreating it would orphan those mounts.
    creds_marker = runner._creds_dir(wt) / "claude" / ".credentials.json"
    creds_marker.parent.mkdir(parents=True)
    creds_marker.write_text("{}")
    runner._write_config(wt, cold=True)
    assert not planted.exists()  # regenerated from scratch
    assert creds_marker.exists()  # bind-mounted creds dir spared


def test_docker_build_and_run_commands(tmp_path: Path, fake_common_dir):
    """The exact docker argv contract: build tags the deterministic (lowercase)
    slug from the config-dir context; run is detached with an init process, the
    stable identity label, and the keepalive command."""
    runner = DockerRunner(Config(isolation="docker"))
    wt = _boxed_worktree(tmp_path, branch="Feature/X")
    slug = runner._slug(wt)
    # Image tags must be lowercase even when the branch name isn't, and unique
    # per worktree path so equal branch names in two repos never collide.
    assert slug == slug.lower() and slug.startswith("treebox-feature--x-")
    other = Worktree(
        "/repo2", "Feature--X", "Feature/X", "main", tmp_path / "elsewhere" / "feature--x"
    )
    assert runner._slug(other) != slug

    config = runner._merged_config(wt, cold=True)
    cfg_dir = runner._config_dir(wt)
    build = runner._build_command(wt, config)
    assert build[:4] == ["docker", "build", "-t", slug]
    assert str(cfg_dir / "Dockerfile") in build  # built from the config dir…
    assert build[-1] == str(cfg_dir)  # …which is also the context
    assert any(a.startswith("USER_UID=") for a in build)

    run = runner._run_command(wt, config)
    assert run[:4] == ["docker", "run", "-d", "--init"]
    assert run[run.index("--name") + 1] == slug
    assert run[run.index("--label") + 1] == f"treebox.workspace={wt.path}"
    assert run[-3:] == [slug, "sleep", "infinity"]


class _FakeStdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_docker_entry_command_execs_by_deterministic_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The printed launch command must work in BOTH worlds: `-i` always (piped
    stdin has to reach the agent), `-t` only when a terminal is attached — a
    hardcoded `-it` would make the --print/--json command die with 'the input
    device is not a TTY' in exactly the scripted contexts it exists for."""
    from treebox.harnesses import get_harness
    from treebox.runners import docker as dr

    runner = DockerRunner(Config(isolation="docker"))
    wt = Worktree("/repo", "wt", "feature/x", "main", tmp_path / "root" / "wt")

    monkeypatch.setattr(dr.sys, "stdin", _FakeStdin(tty=False))
    cmd = runner.entry_command(wt, harness=get_harness("claude"), args=[])
    assert cmd[:3] == ["docker", "exec", "-i"]
    assert cmd[cmd.index("-u") + 1] == "agent"
    assert cmd[cmd.index("-w") + 1] == str(wt.path)
    assert runner._slug(wt) in cmd
    assert cmd[-2:] == ["claude", "--dangerously-skip-permissions"]

    monkeypatch.setattr(dr.sys, "stdin", _FakeStdin(tty=True))
    cmd = runner.entry_command(wt, harness=get_harness("claude"), args=[])
    assert cmd[:4] == ["docker", "exec", "-i", "-t"]


@pytest.mark.parametrize(
    ("harness_name", "argv"),
    [
        ("claude", ["claude", "--dangerously-skip-permissions"]),
        ("codex", ["codex", "--dangerously-bypass-approvals-and-sandbox"]),
    ],
)
def test_autonomous_launch_argv_is_exact(tmp_path: Path, harness_name: str, argv: list[str]):
    """The fully-autonomous launch argv contract: losing a dangerous flag would
    hang every boxed run while the suite stayed green, so assert it exactly for
    both runners and both tools."""
    import shlex

    from treebox.harnesses import get_harness
    from treebox.runners.host import HostRunner

    wt = Worktree("/repo", "wt", "feature/x", "main", tmp_path / "wt")
    harness = get_harness(harness_name)

    # The registry is the single source of the autonomous argv…
    assert harness.launch_argv([]) == argv
    # …and extra agent args are appended verbatim.
    assert harness.launch_argv(["--continue"]) == [*argv, "--continue"]

    host = HostRunner(Config())
    # launch() runs the exact autonomous argv (with cwd=worktree), while the
    # printed/JSON entry command is the same argv wrapped so it carries the
    # worktree directory: pasting it anywhere runs in the box.
    cmd = host.entry_command(wt, harness=harness, args=[])
    assert cmd[:2] == ["sh", "-c"]
    assert cmd[2] == f"cd {shlex.quote(str(wt.path))} && exec {shlex.join(argv)}"
    assert host.entry_command(wt, harness=harness, args=["--continue"])[2].endswith(
        f"exec {shlex.join([*argv, '--continue'])}"
    )

    docker = DockerRunner(Config(isolation="docker"))
    cmd = docker.entry_command(wt, harness=harness, args=[])
    assert cmd[:2] == ["docker", "exec"]
    assert cmd[-len(argv) :] == argv  # same autonomous argv, exec'd in the box


def test_postcreate_is_baked_not_run_from_mount():
    """post-create.sh runs from the image, never from the mounted worktree."""
    import treebox.assets as assets

    tpl = assets.template_dir("default")
    cc = json.loads((tpl / "container.json").read_text())
    assert "/usr/local/bin/post-create.sh" in cc["postCreate"]
    dockerfile = (tpl / "Dockerfile").read_text()
    assert "COPY post-create.sh /usr/local/bin/post-create.sh" in dockerfile


def test_harness_credentials_are_scoped_copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Harness.stage_credentials copies only the whitelisted login files; the
    rest of the live login dir (e.g. host-executed hooks config, project
    history) is never staged, and a stale copy is dropped when the host source
    disappears (copy-or-drop semantics)."""
    from treebox.harnesses import get_harness

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    live = tmp_path / "home" / ".claude"
    live.mkdir(parents=True)
    (live / ".credentials.json").write_text('{"token": "secret"}')
    (live / "settings.json").write_text("{}")
    (live / "history.jsonl").write_text("private\n")  # must NOT be copied

    claude = get_harness("claude")
    assert claude.credential_path() == live  # staging reads the login dir
    assert claude.credentials_present()

    staged = tmp_path / "staged" / "claude"
    claude.stage_credentials(staged)
    assert (staged / ".credentials.json").read_text() == '{"token": "secret"}'
    assert (staged / "settings.json").is_file()
    assert not (staged / "history.jsonl").exists()
    # The staged dir is a copy: nothing was written back into the live dir.
    assert sorted(p.name for p in live.iterdir()) == [
        ".credentials.json",
        "history.jsonl",
        "settings.json",
    ]

    # Host logout ⇒ the stale copy is dropped on the next staging pass.
    (live / ".credentials.json").unlink()
    claude.stage_credentials(staged)
    assert not (staged / ".credentials.json").exists()

    # An absent login dir stages nothing and is reported not-present.
    codex = get_harness("codex")
    assert not codex.credentials_present()
    codex.stage_credentials(tmp_path / "staged" / "codex")
    assert list((tmp_path / "staged" / "codex").iterdir()) == []


def test_get_harness_is_loud_on_unknown_names():
    """The registry resolver mirrors get_runner: an unvalidated name is a loud
    error naming the valid vocabulary, never a silent default."""
    from treebox.harnesses import VALID_HARNESSES, get_harness

    assert get_harness("claude").name == "claude"
    assert get_harness("codex").name == "codex"
    with pytest.raises(ValueError, match="bogus"):
        get_harness("bogus")
    assert VALID_HARNESSES == ("claude", "codex")


def test_docker_refresh_restages_credentials_without_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """refresh() is the always-run entry hook: a fresh host login and a host
    logout both reach the staged copies with no setup run and no dep change —
    auth must never ride the lockfile-hash cache. The copies land outside the
    worktree (only they get mounted, so in-container writes can never touch
    the live host login dirs)."""
    from treebox.output import Reporter

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    host_claude = tmp_path / "home" / ".claude"
    host_claude.mkdir(parents=True)
    (host_claude / ".credentials.json").write_text('{"token": "old"}')
    host_codex = tmp_path / "home" / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "auth.json").write_text("{}")

    runner = DockerRunner(Config(isolation="docker"))
    wt = _boxed_worktree(tmp_path, branch="x")
    runner.refresh(wt, reporter=Reporter(quiet=True))
    staged = runner._creds_dir(wt) / "claude" / ".credentials.json"
    assert staged.read_text() == '{"token": "old"}'
    # refresh() stages EVERY harness in the registry, not just claude.
    assert (runner._creds_dir(wt) / "codex" / "auth.json").is_file()

    # Copies live outside the worktree and outside the live login dir.
    assert wt.path not in staged.parents
    assert host_claude not in staged.parents

    # Host re-login: the new token reaches the copies on the next entry.
    (host_claude / ".credentials.json").write_text('{"token": "new"}')
    runner.refresh(wt, reporter=Reporter(quiet=True))
    assert staged.read_text() == '{"token": "new"}'

    # Host logout (revocation): the stale copy is dropped.
    (host_claude / ".credentials.json").unlink()
    runner.refresh(wt, reporter=Reporter(quiet=True))
    assert not staged.exists()


def test_firewall_template_fails_closed():
    """The firewall overlay grants only the needed capabilities and bakes in the
    env post-create.sh gates on: if the firewall never initialized, workspace
    setup refuses to run rather than running with open egress."""
    import treebox.assets as assets

    tpl = assets.template_dir("default")
    fw = json.loads((tpl / "firewall.json").read_text())
    assert fw["env"]["TREEBOX_FIREWALL"] == "1"
    assert "--cap-add=NET_ADMIN" in fw["runArgs"]

    post_create = (tpl / "post-create.sh").read_text()
    gate = post_create.index("/run/treebox-firewall-ready")
    assert gate < post_create.index("&& uv sync")  # gate precedes workspace code

    init_fw = (tpl / "init-firewall.sh").read_text()
    assert "touch" in init_fw and "/run/treebox-firewall-ready" in init_fw


def test_template_dir_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import treebox.assets as assets

    monkeypatch.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
    monkeypatch.setenv("TREEBOX_HOME", str(tmp_path / "home"))

    # Default falls back to the bundled template.
    default = assets.template_dir("default")
    assert (default / "container.json").is_file()

    # An unknown named template is a loud error, not a silent default.
    with pytest.raises(RuntimeError, match="No template named 'hardened'"):
        assets.template_dir("hardened")

    # A user-authored template under ~/.treebox is picked up by name.
    user = tmp_path / "home" / "templates" / "hardened"
    user.mkdir(parents=True)
    (user / "container.json").write_text("{}")
    assert assets.template_dir("hardened") == user

    # $TREEBOX_TEMPLATE_DIR wins for any name.
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    monkeypatch.setenv("TREEBOX_TEMPLATE_DIR", str(explicit))
    assert assets.template_dir("anything") == explicit


def test_template_dir_tolerates_unresolvable_user(
    monkeypatch: pytest.MonkeyPatch,
):
    import treebox.assets as assets

    marker = "~treebox-user-that-cannot-exist/template"
    monkeypatch.setenv("TREEBOX_TEMPLATE_DIR", marker)

    assert assets.template_dir("anything") == Path(marker)


def test_bundled_template_dir_outlives_the_resolution_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The bundled template may be materialized to a temp location by
    resources.as_file (zipped installs); the returned path must stay valid
    after template_dir returns — held for the process lifetime — and repeated
    resolutions must reuse the same materialization."""
    import treebox.assets as assets

    monkeypatch.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
    monkeypatch.setenv("TREEBOX_HOME", str(tmp_path / "home"))

    first = assets.template_dir("default")
    assert (first / "container.json").is_file()  # usable after the call returned
    assert assets.template_dir("default") == first  # cached, not re-extracted


def test_first_party_imports_are_declared_dependencies():
    """rich is imported directly (output.py, cli.py); it must be a declared
    dependency, not an accident of typer's batteries-included distribution."""
    import tomllib

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    assert any(d.startswith("rich") for d in deps), deps


def test_worktree_lock_is_exclusive(tmp_path: Path):
    from treebox import locking

    repo = str(tmp_path)
    with (
        locking.worktree_lock(repo, "wts", "feature-x"),
        pytest.raises(locking.LockError),
        locking.worktree_lock(repo, "wts", "feature-x"),
    ):
        pass
    # Released after the first block: re-acquiring now succeeds.
    with locking.worktree_lock(repo, "wts", "feature-x"):
        pass


# --- docker runner: setup / teardown against a canned docker CLI ----------------


class _FakeDocker:
    """Canned docker CLI injected at the runner constructor (the internal
    engine seam): records every argv and answers ``info``/``ps``/``inspect``
    queries from scripted output."""

    def __init__(
        self,
        *,
        ids: str = "",
        image: str = "",
        volumes: str = "",
        name: str = "",
        running: bool = True,
        env: str = "",
        volume_ls: str = "",
        info_rc: int = 0,
        failures: dict[tuple[str, ...], str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._ids = ids
        self._image = image
        self._volumes = volumes
        self._name = name
        self._running = running
        self._env = env
        self._volume_ls = volume_ls
        self._info_rc = info_rc
        self._failures = failures or {}

    def __call__(self, args: list[str]):
        import subprocess

        self.calls.append(list(args))
        stdout = ""
        stderr = ""
        rc = 0
        if args[0] == "info":
            rc = self._info_rc  # daemon down <=> non-zero `docker info`
        elif args[0] == "ps":
            stdout = self._ids
        elif args[:2] == ["volume", "ls"]:
            stdout = self._volume_ls
        elif args[0] == "inspect":
            fmt = args[-1]
            if fmt == "{{.Config.Image}}":
                stdout = self._image
            elif ".State.Running" in fmt:  # the combined _container_state format
                running = "true" if self._running else "false"
                stdout = f"/{self._name}\n{running}\n{self._env}"
            else:
                stdout = self._volumes
        failure = self._failures.get(tuple(args))
        if failure is not None:
            rc = 1
            stderr = failure
        return subprocess.CompletedProcess(["docker", *args], rc, stdout, stderr)


class _RecordingReporter:
    """Captures ``step`` argvs instead of running them, so setup's command
    order can be asserted without a docker daemon. Quacks like Reporter for
    the methods the runner touches."""

    def __init__(self) -> None:
        self.steps: list[list[str]] = []
        self.notes: list[str] = []
        self.warnings: list[str] = []

    def step(self, label, detail, argv, *, cwd=None, env=None):
        self.steps.append(list(argv))
        return ""

    def note(self, label, detail=""):
        self.notes.append(label)

    def warn(self, msg):
        self.warnings.append(msg)

    @contextmanager
    def task(self, label, detail="done"):
        yield


def test_docker_setup_runs_firewall_before_workspace_setup(tmp_path: Path, fake_common_dir):
    """Default-deny egress must exist before any workspace-derived code runs:
    setup execs init-firewall.sh (as root) before post-create.sh, and the
    container is created via docker run with the keepalive command."""
    fake = _FakeDocker(ids="")  # no existing container → fresh create
    runner = DockerRunner(Config(isolation="docker", firewall=True), docker=fake)
    wt = _boxed_worktree(tmp_path)
    rep = _RecordingReporter()

    runner.setup(wt, cold=True, reporter=rep)

    assert rep.steps[0][:2] == ["docker", "build"]
    joined = [" ".join(argv) for argv in rep.steps]
    fw = next(i for i, a in enumerate(joined) if "init-firewall.sh" in a)
    pc = next(i for i, a in enumerate(joined) if "post-create.sh" in a)
    assert fw < pc
    assert rep.steps[fw][rep.steps[fw].index("-u") + 1] == "root"
    # The container itself was created through the seam, detached + kept alive.
    run = next(c for c in fake.calls if c[0] == "run")
    assert run[-2:] == ["sleep", "infinity"]


def test_docker_setup_without_firewall_skips_the_lockdown_exec(tmp_path: Path, fake_common_dir):
    fake = _FakeDocker(ids="")
    runner = DockerRunner(Config(isolation="docker"), docker=fake)
    rep = _RecordingReporter()

    runner.setup(_boxed_worktree(tmp_path), cold=True, reporter=rep)

    assert not any("init-firewall.sh" in " ".join(a) for a in rep.steps)
    assert any("post-create.sh" in " ".join(a) for a in rep.steps)


def test_docker_setup_reuses_existing_container(tmp_path: Path, fake_common_dir):
    """An existing labeled container is reused — started if stopped, firewall
    re-established from its baked env — never rebuilt or re-run."""
    cfg = Config(isolation="docker")
    wt = _boxed_worktree(tmp_path)
    slug = DockerRunner(cfg)._slug(wt)
    fake = _FakeDocker(ids="abc123\n", name=slug, running=False, env="TREEBOX_FIREWALL=1\n")
    runner = DockerRunner(cfg, docker=fake)
    rep = _RecordingReporter()

    runner.setup(wt, cold=True, reporter=rep)

    assert ["start", "abc123"] in fake.calls
    assert not any(c[0] == "run" for c in fake.calls)
    assert not any(a[:2] == ["docker", "build"] for a in rep.steps)
    # Rules don't survive a restart: the stopped firewall container re-inits
    # before post-create re-syncs deps.
    joined = [" ".join(argv) for argv in rep.steps]
    assert any("init-firewall.sh" in a for a in joined)


def test_docker_prepare_entry_restarts_stopped_container_and_relocks_egress(
    tmp_path: Path, fake_common_dir
):
    """iptables rules don't survive a restart: prepare_entry on a stopped
    container must docker-start it and re-run the guarded firewall init, in
    that order - a hand-run `docker start` would leave egress silently open."""
    cfg = Config(isolation="docker")
    wt = _boxed_worktree(tmp_path)
    slug = DockerRunner(cfg)._slug(wt)
    fake = _FakeDocker(ids="abc123\n", name=slug, running=False)

    DockerRunner(cfg, docker=fake).prepare_entry(wt)

    start = fake.calls.index(["start", "abc123"])
    firewall = next(i for i, c in enumerate(fake.calls) if "init-firewall.sh" in " ".join(c))
    assert start < firewall


def test_docker_prepare_entry_is_a_noop_when_already_running(tmp_path: Path, fake_common_dir):
    cfg = Config(isolation="docker")
    wt = _boxed_worktree(tmp_path)
    slug = DockerRunner(cfg)._slug(wt)
    fake = _FakeDocker(ids="abc123\n", name=slug, running=True)

    DockerRunner(cfg, docker=fake).prepare_entry(wt)

    assert not any(c[0] == "start" or "init-firewall.sh" in " ".join(c) for c in fake.calls)


def test_docker_prepare_entry_without_container_points_at_recreate(tmp_path: Path, fake_common_dir):
    fake = _FakeDocker(ids="")
    runner = DockerRunner(Config(isolation="docker"), docker=fake)

    with pytest.raises(RuntimeError, match="treebox teardown"):
        runner.prepare_entry(_boxed_worktree(tmp_path))


def test_host_prepare_entry_is_a_noop(tmp_path: Path):
    """The host runner needs no entry readiness: an emitted entry_command
    works whenever the worktree exists."""
    from treebox.runners.host import HostRunner

    assert HostRunner(Config()).prepare_entry(_boxed_worktree(tmp_path)) is None


def test_docker_setup_refuses_foreign_container(tmp_path: Path, fake_common_dir):
    """A labeled container this runner didn't create is an explicit error
    pointing at teardown — not silently adopted with the wrong
    name/mounts/user."""
    fake = _FakeDocker(ids="abc123\n", name="funny_wozniak")
    runner = DockerRunner(Config(isolation="docker"), docker=fake)
    rep = _RecordingReporter()

    with pytest.raises(RuntimeError, match="treebox teardown"):
        runner.setup(_boxed_worktree(tmp_path), cold=True, reporter=rep)


def test_docker_setup_refuses_firewall_on_unfirewalled_container(tmp_path: Path, fake_common_dir):
    """Capabilities can't be added to an existing container: requesting the
    firewall for a container created without it must fail loudly — never
    silently run workspace setup with open egress."""
    cfg = Config(isolation="docker", firewall=True)
    wt = _boxed_worktree(tmp_path)
    slug = DockerRunner(cfg)._slug(wt)
    fake = _FakeDocker(ids="abc123\n", name=slug, running=True, env="PATH=/bin\n")
    runner = DockerRunner(cfg, docker=fake)

    with pytest.raises(RuntimeError, match="created without it"):
        runner.setup(wt, cold=True, reporter=_RecordingReporter())


def _reconcile(cfg, st, **kw):
    """_reconcile_with_state with the reporter/flag boilerplate defaulted, so
    each policy assertion below reads as one line of the precedence table."""
    from treebox.cli import _reconcile_with_state
    from treebox.output import Reporter

    kw.setdefault("isolation", None)
    return _reconcile_with_state(Reporter(quiet=True), cfg, st, **kw)


def test_reconcile_honors_recorded_no_firewall_over_config_default(tmp_path: Path, fake_common_dir):
    """A worktree created with --no-firewall under a firewall=true config must
    reconnect cleanly: enter resolves the firewall from the recorded created-time
    choice, so setup against its unfirewalled container never hard-errors."""
    from treebox.state import WorktreeState

    st = WorktreeState(base="main", isolation="docker", harness="claude", firewall=False)
    cfg = _reconcile(Config(isolation="docker", firewall=True), st, honor_firewall=True)
    assert cfg.firewall is False  # recorded created-time choice wins over the default

    wt = _boxed_worktree(tmp_path)
    fake = _FakeDocker(
        ids="abc123\n", name=DockerRunner(cfg)._slug(wt), running=True, env="PATH=/bin\n"
    )

    # No RuntimeError: the resolved firewall matches the container's own state.
    DockerRunner(cfg, docker=fake).setup(wt, cold=True, reporter=_RecordingReporter())


def test_reconcile_firewall_uses_config_default_without_state():
    """With no state to read (a worktree whose dir is gone), `enter` keeps the
    config default instead of inventing a firewall choice — and teardown
    (honor_firewall=False) always keeps the config default."""
    from treebox.state import WorktreeState

    assert _reconcile(Config(firewall=True), None, honor_firewall=True).firewall is True
    assert _reconcile(Config(firewall=False), None, honor_firewall=True).firewall is False
    # The teardown flow never reconciles the firewall, recorded or not.
    recorded = WorktreeState(base="main", isolation="host", harness="claude", firewall=True)
    assert _reconcile(Config(firewall=False), recorded).firewall is False


def test_reconcile_honors_recorded_template_over_config_default():
    """A worktree created with `--template hardened` must re-enter against that
    same template even when the config default is `default` and no `--template`
    is passed: otherwise a deps-changing enter regenerates container.json from
    the wrong template (wrong user → unauthenticated agent)."""
    from treebox.state import WorktreeState

    st = WorktreeState(base="main", isolation="docker", harness="claude", template="hardened")
    # No --template: the recorded created-time template wins over the default.
    assert _reconcile(Config(template="default"), st).template == "hardened"
    # An explicit --template stays a legitimate per-session override.
    assert _reconcile(Config(template="default"), st, template="other").template == "default"


def test_reconcile_template_uses_config_default_when_unrecorded():
    """An unrecorded template (None) must not force a choice: enter falls back
    to the config default."""
    from treebox.state import WorktreeState

    unrecorded = WorktreeState(base="main", isolation="docker", harness="claude")
    assert unrecorded.template is None
    assert _reconcile(Config(template="default"), unrecorded).template == "default"
    assert _reconcile(Config(template="custom"), None).template == "custom"


def test_reconcile_isolation_and_harness_policy():
    """The remaining rows of the precedence table, at the unit level (the CLI
    paths — conflict exit codes, recorded-harness launches — are pinned by the
    contract and integration suites): recorded isolation wins over the config
    default; a matching explicit --isolation is fine; a known recorded harness
    wins unless -H overrides; an unknown recorded harness falls back."""
    from treebox.state import WorktreeState

    st = WorktreeState(base="main", isolation="docker", harness="codex")
    out = _reconcile(Config(isolation="host", harness="claude"), st)
    assert out.isolation == "docker"  # recorded wins over the config default
    assert out.harness == "codex"  # recorded, known -> wins

    # A matching explicit flag is not a conflict, and -H stays a per-session
    # override. (In the real flow the explicit values are already folded into
    # cfg by _resolve_config; reconcile just declines to second-guess them.)
    out = _reconcile(
        Config(isolation="docker", harness="claude"), st, isolation="docker", harness="claude"
    )
    assert out.isolation == "docker" and out.harness == "claude"

    # Unknown recorded harness falls back to the config default; no state at
    # all leaves the config untouched.
    weird = WorktreeState(base="main", isolation="host", harness="aider")
    assert _reconcile(Config(harness="claude"), weird).harness == "claude"
    assert _reconcile(Config(isolation="host"), None).isolation == "host"


def test_docker_setup_cold_reuse_warns_about_frozen_mounts(tmp_path: Path, fake_common_dir):
    """--cold cannot strip cache mounts from an already-created container;
    say so instead of silently reporting a cold sync that wasn't."""
    cfg = Config(isolation="docker")
    wt = _boxed_worktree(tmp_path)
    fake = _FakeDocker(ids="abc123\n", name=DockerRunner(cfg)._slug(wt), running=True)
    runner = DockerRunner(cfg, docker=fake)
    rep = _RecordingReporter()

    runner.setup(wt, cold=True, reporter=rep)

    assert any("creation-time cache mounts" in w for w in rep.warnings)


def test_docker_rejects_unknown_template_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_common_dir
):
    """Unknown keys (typos) in an operator template are a loud error listing
    the supported keys — silently dropping them would strip the env/setup
    behavior the operator relies on."""
    tpl = tmp_path / "tpl"
    tpl.mkdir()
    (tpl / "container.json").write_text(json.dumps({"users": "agent", "postcreate": "setup.sh"}))
    monkeypatch.setenv("TREEBOX_TEMPLATE_DIR", str(tpl))
    runner = DockerRunner(Config(isolation="docker"))

    with pytest.raises(RuntimeError, match="Unsupported key"):
        runner._merged_config(_boxed_worktree(tmp_path), cold=True)


def test_docker_dry_run_is_side_effect_free(tmp_path: Path, fake_common_dir):
    """--dry-run must change nothing: rendering the plan may not create the
    configured cache directories (that happens in setup, before docker run)."""
    cache = tmp_path / "not-yet" / "uv"
    runner = DockerRunner(Config(isolation="docker", caches={"uv": str(cache)}))
    wt = _boxed_worktree(tmp_path)

    cmds = runner.dry_run_setup(wt)

    assert not cache.exists()
    assert any(c.startswith("docker build") for c in cmds)
    assert any(c.startswith("docker run") for c in cmds)


def test_docker_mount_paths_with_commas_are_a_loud_error(tmp_path: Path, fake_common_dir):
    """docker --mount syntax cannot express a comma; fail with a clear message
    instead of a baffling daemon error after the image build already ran."""
    runner = DockerRunner(Config(isolation="docker"))
    wt_path = tmp_path / "root" / "feat,fast"
    wt_path.mkdir(parents=True)
    wt = Worktree("/repo", "feat,fast", "feat,fast", "main", wt_path)

    with pytest.raises(RuntimeError, match="containing ','"):
        runner._merged_config(wt, cold=True)


def test_config_rejects_unknown_isolation(tmp_path: Path):
    """No legacy isolation aliases: a config.toml naming an isolation mode that
    doesn't exist fails loudly instead of being silently mapped to another."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('isolation = "bogus"\n')
    with pytest.raises(ValueError, match="Invalid isolation"):
        load_config(cfg_file)


def test_docker_teardown_removes_own_image_and_treebox_volumes(tmp_path: Path):
    """The magic-prefix contracts: only images treebox built (treebox-*) and
    treebox-named volumes are removed; foreign volumes are never touched."""
    from treebox.output import Reporter

    image = "treebox-feature--x-deadbeef00"
    fake = _FakeDocker(
        ids="abc123\n",
        image=f"{image}\n",
        volumes="treebox-claude-config\nunrelated-volume\n",
    )
    runner = DockerRunner(Config(isolation="docker"), remove_volumes=True, docker=fake)
    wt = _boxed_worktree(tmp_path)

    result = runner.teardown(wt, reporter=Reporter(quiet=True))

    assert result.container == "cleaned"
    assert result.volumes_removed is True
    assert ["rm", "-f", "abc123"] in fake.calls
    assert ["image", "rm", image] in fake.calls
    volume_rms = [c for c in fake.calls if c[:2] == ["volume", "rm"]]
    assert volume_rms == [["volume", "rm", "treebox-claude-config"]]


def test_docker_teardown_keeps_foreign_image_and_volumes_by_default(tmp_path: Path):
    """Without --remove-volumes no volume is touched, and an image treebox did
    not build (operator-pinned) is never removed."""
    from treebox.output import Reporter

    fake = _FakeDocker(ids="abc123\n", image="python:3.14\n", volumes="treebox-x\n")
    runner = DockerRunner(Config(isolation="docker"), docker=fake)
    wt = _boxed_worktree(tmp_path)

    result = runner.teardown(wt, reporter=Reporter(quiet=True))

    assert result.container == "cleaned"
    assert result.volumes_removed is False
    assert ["rm", "-f", "abc123"] in fake.calls
    assert not any(c[:2] == ["image", "rm"] for c in fake.calls)
    assert not any(c[:2] == ["volume", "rm"] for c in fake.calls)


def test_docker_teardown_skips_when_docker_unavailable(tmp_path: Path):
    from treebox.output import Reporter

    fake = _FakeDocker(info_rc=1)  # daemon unreachable
    runner = DockerRunner(Config(isolation="docker"), remove_volumes=True, docker=fake)
    wt = _boxed_worktree(tmp_path)

    result = runner.teardown(wt, reporter=Reporter(quiet=True))

    assert result.container == "skipped"
    assert result.volumes_removed is False
    # No daemon → nothing beyond the availability probe touches docker.
    assert fake.calls == [["info"]]


@pytest.mark.parametrize(
    ("failed_command", "expected_container", "expected_volumes_removed", "image_rm_attempted"),
    [
        (("rm", "-f", "abc123"), "failed", True, False),
        (("image", "rm", "treebox-feature--x-deadbeef00"), "failed", True, True),
        (("volume", "rm", "treebox-claude-config"), "cleaned", False, True),
    ],
)
def test_docker_teardown_continues_past_destructive_command_failure(
    tmp_path: Path,
    failed_command: tuple[str, ...],
    expected_container: str,
    expected_volumes_removed: bool,
    image_rm_attempted: bool,
):
    """One failed removal must not abort the rest of the cleanup — the caller
    deletes the state that could retry it right after teardown, so every
    remaining step still runs (only a doomed image rm after a failed container
    rm is skipped) and the result reports each resource's outcome honestly:
    a failed volume rm alone leaves the container status accurate."""
    from treebox.output import Reporter

    image = "treebox-feature--x-deadbeef00"
    fake = _FakeDocker(
        ids="abc123\n",
        image=f"{image}\n",
        volumes="treebox-claude-config\n",
        failures={failed_command: "removal denied"},
    )
    runner = DockerRunner(Config(isolation="docker"), remove_volumes=True, docker=fake)
    wt = _boxed_worktree(tmp_path)
    cfg_dir = runner._config_dir(wt)
    cfg_dir.mkdir(parents=True)

    result = runner.teardown(wt, reporter=Reporter(quiet=True))

    assert result.container == expected_container
    assert result.volumes_removed is expected_volumes_removed
    assert list(failed_command) in fake.calls
    assert (["image", "rm", image] in fake.calls) is image_rm_attempted
    assert ["volume", "rm", "treebox-claude-config"] in fake.calls
    assert not cfg_dir.exists()


def test_docker_teardown_no_container_removes_config_dir(tmp_path: Path):
    """With no matching container, teardown only queries docker (availability,
    container and volume lookups; nothing removed) — and still removes the
    host-side operator config dir."""
    from treebox.output import Reporter

    fake = _FakeDocker(ids="")
    runner = DockerRunner(Config(isolation="docker"), remove_volumes=True, docker=fake)
    wt = _boxed_worktree(tmp_path)
    cfg_dir = runner._config_dir(wt)
    cfg_dir.mkdir(parents=True)

    runner.teardown(wt, reporter=Reporter(quiet=True))

    assert fake.calls[:2] == [
        ["info"],
        ["ps", "-aq", "--filter", f"label=treebox.workspace={wt.path}"],
    ]
    assert all(c[0] in ("info", "ps", "volume") for c in fake.calls)
    assert not any(c[:2] == ["volume", "rm"] for c in fake.calls)
    assert not cfg_dir.exists()


def test_docker_teardown_finds_volumes_when_container_already_gone(tmp_path: Path, fake_common_dir):
    """--remove-volumes must not depend on a live container's mount table: the
    workspace-named volumes from the template's ${workspaceName} substitution
    are discovered and removed even after a manual `docker rm` — previously
    they leaked forever, with no treebox way to clean them up."""
    from treebox.output import Reporter
    from treebox.runners.docker import _sanitize

    vol = f"treebox-shellhistory-{_sanitize('feature--x')}"
    fake = _FakeDocker(ids="", volume_ls=f"{vol}\nunrelated-volume\n")
    runner = DockerRunner(Config(isolation="docker"), remove_volumes=True, docker=fake)
    wt = _boxed_worktree(tmp_path)

    runner.teardown(wt, reporter=Reporter(quiet=True))

    volume_rms = [c for c in fake.calls if c[:2] == ["volume", "rm"]]
    assert volume_rms == [["volume", "rm", vol]]  # never the unrelated volume


def test_docker_teardown_leaves_shared_template_volume_when_container_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A template volume with a static (no-${workspaceName}) source is shared
    across worktrees, so a single-worktree teardown --remove-volumes must never
    reclaim it — even with the container already gone. The per-workspace volume
    beside it is still reclaimed."""
    from treebox.output import Reporter
    from treebox.runners.docker import _sanitize

    per_ws = f"treebox-shellhistory-{_sanitize('feature--x')}"
    shared = "treebox-team-cache"
    monkeypatch.setattr(
        DockerRunner,
        "_overlaid_config",
        lambda self: {
            "mounts": [
                "type=volume,source=treebox-shellhistory-${workspaceName},target=/commandhistory",
                f"type=volume,source={shared},target=/cache",
            ]
        },
    )
    fake = _FakeDocker(ids="", volume_ls=f"{per_ws}\n{shared}\n")
    runner = DockerRunner(Config(isolation="docker"), remove_volumes=True, docker=fake)
    wt = _boxed_worktree(tmp_path)

    runner.teardown(wt, reporter=Reporter(quiet=True))

    volume_rms = [c for c in fake.calls if c[:2] == ["volume", "rm"]]
    assert volume_rms == [["volume", "rm", per_ws]]  # shared volume untouched


def test_teardown_runner_recovers_created_time_template(tmp_path, monkeypatch):
    """teardown has no --template flag, so its runner must recover the created-time
    template from state: otherwise `_template_volumes` derives the config-default
    template's volume names and the custom template's ${workspaceName} volume
    leaks (the residual gap #109's fix couldn't close without the recorded name)."""
    from treebox import git, state
    from treebox.cli import _teardown_runner
    from treebox.config import Config
    from treebox.output import Reporter
    from treebox.resolve import Candidate

    monkeypatch.setattr(git, "git_dir", lambda p: str(tmp_path))
    monkeypatch.setattr(git, "registered_gitdir", lambda repo, p: tmp_path)
    state.save(
        tmp_path,
        state.WorktreeState(base="main", isolation="docker", harness="claude", template="hardened"),
    )
    cand = Candidate(name="work", branch="treebox/work", path=str(tmp_path))

    run = _teardown_runner(
        Reporter(quiet=True),
        Config(isolation="docker", template="default"),
        cand,
        str(tmp_path),
        explicit=None,
        remove_volumes=True,
        json_out=False,
    )
    assert run is not None
    assert run.config.template == "hardened"  # recovered from state, not the config default


def test_docker_doctor_gates_on_binary_and_daemon(monkeypatch: pytest.MonkeyPatch):
    """doctor is the only safety gate before a sandboxed create: each missing
    dependency must raise its own loud PreflightError carrying a stable machine
    code and a remediation hint. Docker is the whole dependency: the binary and
    the daemon are the only gates."""
    from treebox.output import Reporter
    from treebox.runners import docker as dr
    from treebox.runners.base import PreflightError

    runner = DockerRunner(Config(isolation="docker"))
    rep = Reporter(quiet=True)

    monkeypatch.setattr(dr.system, "have", lambda c: False)
    with pytest.raises(PreflightError, match="needs docker") as exc_info:
        runner.preflight(rep)
    assert exc_info.value.error_code == "MISSING_DEPENDENCY"
    assert exc_info.value.hint

    monkeypatch.setattr(dr.system, "have", lambda c: True)
    monkeypatch.setattr(dr, "_docker_available", lambda: False)
    with pytest.raises(PreflightError, match="daemon is not reachable") as exc_info:
        runner.preflight(rep)
    assert exc_info.value.error_code == "DOCKER_UNAVAILABLE"
    assert "docker info" in (exc_info.value.hint or "")

    monkeypatch.setattr(dr, "_docker_available", lambda: True)
    runner.preflight(rep)  # all dependencies present → no raise


def test_docker_preflight_hints_are_platform_aware(monkeypatch: pytest.MonkeyPatch):
    """A fresh macOS box gets Docker Desktop / colima guidance; Linux gets
    Docker Engine + systemctl."""
    from treebox.runners import docker as dr

    monkeypatch.setattr(dr.sys, "platform", "darwin")
    assert "Docker Desktop" in dr._install_hint() and "colima" in dr._install_hint()
    assert "Docker Desktop" in dr._daemon_hint()

    monkeypatch.setattr(dr.sys, "platform", "linux")
    assert "docs.docker.com/engine/install" in dr._install_hint()
    assert "systemctl start docker" in dr._daemon_hint()


def test_docker_cold_skips_cache_mounts(tmp_path: Path, fake_common_dir):
    cfg = Config(isolation="docker", caches={"uv": str(tmp_path / "uvc")})
    runner = DockerRunner(cfg)
    wt = _boxed_worktree(tmp_path, branch="x")
    runner._write_config(wt, cold=True)
    data = json.loads(runner._config_file(wt).read_text())
    assert not any("/caches/uv" in m for m in data["mounts"])
    # Auth is not a cache: credential-copy mounts must survive cold mode…
    assert any("/home/agent/.claude" in m for m in data["mounts"])
    # …and so must the workspace + git mounts.
    assert any(f"target={wt.path}" in m for m in data["mounts"])


def test_copy_env_does_not_follow_a_planted_symlink(tmp_path: Path):
    # A malicious branch (or boxed agent) can plant <worktree>/.env as a symlink
    # pointing outside the worktree. The host-side copy must not write the user's
    # secrets through it; it must replace the symlink with a regular file instead.
    from treebox.output import Reporter
    from treebox.provision import copy_env

    canonical = tmp_path / "canonical.env"
    canonical.write_text("SECRET=canonical\n")

    outside = tmp_path / "victim.txt"
    outside.write_text("untouched\n")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".env").symlink_to(outside)

    assert copy_env(str(worktree), worktree, str(canonical), Reporter()) is True

    # The out-of-worktree file the symlink pointed at is never written through.
    assert outside.read_text() == "untouched\n"
    # The destination is now a real file holding the secrets, not a symlink.
    assert not (worktree / ".env").is_symlink()
    assert (worktree / ".env").read_text() == "SECRET=canonical\n"


def test_copy_submodules_skips_escaping_gitmodules_paths(tmp_path: Path):
    """``.gitmodules`` is untrusted: ``..``/absolute paths and symlinks must
    never make the copy read outside the repo or write outside the worktree."""
    from treebox.output import Reporter
    from treebox.provision import copy_submodules

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitmodules").write_text(
        '[submodule "up"]\n\tpath = ../outside\n\turl = https://example.com/x\n'
        f'[submodule "abs"]\n\tpath = {outside}\n\turl = https://example.com/x\n'
        '[submodule "link"]\n\tpath = vendor\n\turl = https://example.com/x\n'
        '[submodule "deep"]\n\tpath = pkg/inner\n\turl = https://example.com/x\n'
        '[submodule "ok"]\n\tpath = sub\n\turl = https://example.com/x\n'
    )
    (repo / "vendor").symlink_to(outside)  # committed top-level symlink escape
    (repo / "pkg" / "inner").mkdir(parents=True)
    (repo / "sub").mkdir()
    (repo / "sub" / "lib.txt").write_text("legit\n")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    # A checked-out branch can plant a symlink where a submodule's parent goes.
    (worktree / "pkg").symlink_to(outside)

    copied = copy_submodules(str(repo), worktree, Reporter())

    # Only the honest submodule is copied; every escape vector is skipped.
    assert copied == 1
    assert (worktree / "sub" / "lib.txt").read_text() == "legit\n"
    assert not (worktree / "vendor").exists()
    # Nothing was written through the symlinked destination parent.
    assert list(outside.iterdir()) == [outside / "secret.txt"]


def test_copy_env_is_force_ignored_in_the_worktree(tmp_path: Path):
    """The repo's .gitignore is untrusted; treebox itself must guarantee the
    copied .env can never be staged by an autonomous ``git add -A``."""
    import subprocess

    from treebox.output import Reporter
    from treebox.provision import copy_env

    canonical = tmp_path / "canonical.env"
    canonical.write_text("SECRET=canonical\n")

    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)

    assert copy_env(str(wt), wt, str(canonical), Reporter()) is True
    assert copy_env(str(wt), wt, str(canonical), Reporter()) is True  # idempotent

    # git itself now refuses to see .env, regardless of any .gitignore.
    check = subprocess.run(["git", "-C", str(wt), "check-ignore", "-q", ".env"])
    assert check.returncode == 0
    exclude = wt / ".git" / "info" / "exclude"
    assert exclude.read_text().splitlines().count("/.env") == 1


def test_git_argv_uses_end_of_options_separator(monkeypatch: pytest.MonkeyPatch):
    """Positional refs/paths handed to git are preceded by ``--`` so a
    flag-shaped value can never be parsed as an option."""
    from treebox import git

    calls: list[list[str]] = []
    monkeypatch.setattr(git, "_run", lambda args, **kw: calls.append(list(args)) or "")

    git.worktree_add("/r", "/wt", git.BranchPlan("local", "b", None))
    git.worktree_add("/r", "/wt", git.BranchPlan("new", "b", "--detach"))
    git.delete_branch("/r", "b")

    local, new, delete = calls
    assert local[-3:] == ["--", "/wt", "b"]
    assert new[-5:] == ["-b", "b", "--", "/wt", "--detach"]
    assert delete[-2:] == ["--", "b"]


def test_state_provisioned_roundtrips(tmp_path, monkeypatch):
    """The provisioned flag round-trips; a state file missing the key (corrupt or
    truncated) reads as not-provisioned rather than silently half-built."""
    import json

    from treebox import git, state

    monkeypatch.setattr(git, "git_dir", lambda p: str(tmp_path))

    state.save(tmp_path, state.WorktreeState(base="main", isolation="docker", harness="claude"))
    assert state.load(tmp_path).provisioned is False  # our default for fresh objects

    state.save(
        tmp_path,
        state.WorktreeState(base="main", isolation="docker", harness="claude", provisioned=True),
    )
    assert state.load(tmp_path).provisioned is True

    # A file missing the "provisioned" key reads as not-provisioned.
    (tmp_path / "treebox-state.json").write_text(
        json.dumps({"base": "main", "isolation": "docker", "harness": "claude"})
    )
    assert state.load(tmp_path).provisioned is False


def test_state_firewall_roundtrips(tmp_path, monkeypatch):
    """The recorded firewall choice round-trips so `enter` honors the created-time
    decision; fresh objects and a file missing the key both read as False."""
    import json

    from treebox import git, state

    monkeypatch.setattr(git, "git_dir", lambda p: str(tmp_path))

    for choice in (True, False):
        state.save(
            tmp_path,
            state.WorktreeState(base="main", isolation="docker", harness="claude", firewall=choice),
        )
        assert state.load(tmp_path).firewall is choice

    # Fresh objects and files missing the "firewall" key both read as False.
    assert state.WorktreeState(base="main", isolation="docker", harness="claude").firewall is False
    (tmp_path / "treebox-state.json").write_text(
        json.dumps({"base": "main", "isolation": "docker", "harness": "claude"})
    )
    assert state.load(tmp_path).firewall is False


def test_state_template_roundtrips(tmp_path, monkeypatch):
    """The recorded template round-trips so `enter`/`teardown` render the same
    container; an unrecorded template (no key) reads as None so the caller falls
    back to the config default rather than drifting to a wrong template."""
    import json

    from treebox import git, state

    monkeypatch.setattr(git, "git_dir", lambda p: str(tmp_path))

    for tpl in ("hardened", "default"):
        state.save(
            tmp_path,
            state.WorktreeState(base="main", isolation="docker", harness="claude", template=tpl),
        )
        assert state.load(tmp_path).template == tpl

    # Fresh objects and files missing the "template" key both read as None.
    assert state.WorktreeState(base="main", isolation="docker", harness="claude").template is None
    (tmp_path / "treebox-state.json").write_text(
        json.dumps({"base": "main", "isolation": "docker", "harness": "claude"})
    )
    assert state.load(tmp_path).template is None


def test_host_git_pins_exec_shaped_config(monkeypatch: pytest.MonkeyPatch):
    """Every host-side git call pins the exec-shaped config keys to inert
    values. The docker runner mounts the repo's git common dir writable, so a
    boxed agent can write the shared .git/config; pinning these means a hostile
    core.hooksPath / core.fsmonitor planted there can't run code when treebox
    itself invokes git on the host. The agent's in-container git is untouched."""
    from treebox import git

    captured: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        captured.append(list(argv))
        return _Proc()

    monkeypatch.setattr(git.subprocess, "run", fake_run)

    git.is_dirty("/wt")  # routed through the shared _run seam
    git.has_origin("/r")  # a direct subprocess.run site

    assert captured  # both calls landed
    n = len(git._SAFE_CONFIG)
    for argv in captured:
        # Pinned as the literal prefix right after "git", ahead of -C and the
        # subcommand, so they always take effect.
        assert argv[0] == "git"
        assert tuple(argv[1 : 1 + n]) == git._SAFE_CONFIG
        assert "core.hooksPath=/dev/null" in git._SAFE_CONFIG
        assert "core.fsmonitor=false" in git._SAFE_CONFIG


# --- cli: --json output contract ----------------------------------------------

from typer.testing import CliRunner  # noqa: E402

from treebox.cli import SCHEMA_VERSION, app  # noqa: E402

cli_runner = CliRunner()


def _cli(args: list[str]):
    return cli_runner.invoke(app, args, catch_exceptions=False)


@pytest.fixture
def no_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config discovery at an empty file so tests never read the
    developer's real ~/.treebox/config.toml. (It must exist: an
    explicitly-set $TREEBOX_CONFIG naming a missing file is a loud error.)"""
    cfg = tmp_path / "empty-config.toml"
    cfg.write_text("")
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))


def test_emit_json_serialization_is_defined_once():
    # One emitter owns the format: indent=2 + trailing newline, success and
    # error payloads alike.
    import io

    from treebox.cli import _emit_json

    buf = io.StringIO()
    payload = {"schemaVersion": 1, "error": {"code": "X", "message": "m"}}
    _emit_json(payload, stream=buf)
    assert buf.getvalue() == json.dumps(payload, indent=2) + "\n"


def test_create_root_flag_expands_quoted_tilde_in_dry_run_json(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_user_config
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    res = _cli(
        [
            "create",
            "tilde-root",
            "--repo",
            str(repo),
            "--root",
            "~/cli-wts",
            "--dry-run",
            "--no-fetch",
            "--json",
        ]
    )

    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["worktree_path"] == str(home / "cli-wts" / "tilde-root")


def test_repo_flag_expands_quoted_tilde(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_user_config
):
    # Without expansion the literal ~/repo is NOT_A_REPO and exits 2.
    monkeypatch.setenv("HOME", str(tmp_path))

    res = _cli(["list", "--repo", "~/repo", "--json"])

    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["worktrees"] == []


def test_invalid_config_is_clean_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A one-character typo in the user's TOML must not dump a traceback: every
    # command exits 2 (EXIT_USAGE) with a styled message instead.
    bad = tmp_path / "config.toml"
    bad.write_text('isolation = "nope"\n')
    monkeypatch.setenv("TREEBOX_CONFIG", str(bad))
    res = _cli(["list"])
    assert res.exit_code == 2
    assert "Traceback" not in res.output
    assert "Invalid isolation" in res.output


def test_invalid_config_json_error_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # In --json mode the same failure emits the structured error payload agents
    # branch on (error.code == INVALID_CONFIG), on stderr.
    bad = tmp_path / "config.toml"
    bad.write_text("this is not toml [\n")
    monkeypatch.setenv("TREEBOX_CONFIG", str(bad))
    res = _cli(["list", "--json"])
    assert res.exit_code == 2
    err = json.loads(res.stderr)
    assert err["schemaVersion"] == SCHEMA_VERSION
    assert err["error"]["code"] == "INVALID_CONFIG"
    assert "hint" in err["error"]
    assert res.stdout == ""


def test_explicit_missing_config_is_a_loud_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # An explicitly-set $TREEBOX_CONFIG asserts the file exists: a typo'd path
    # must be INVALID_CONFIG (exit 2), never a silent run on built-in defaults
    # with the wrong isolation/base/caches.
    monkeypatch.setenv("TREEBOX_CONFIG", str(tmp_path / "typo.toml"))
    res = _cli(["list", "--json"])
    assert res.exit_code == 2
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "INVALID_CONFIG"
    assert "TREEBOX_CONFIG" in err["error"]["message"]
    assert "hint" in err["error"]


def test_path_env_overrides_tolerate_unresolvable_user(
    monkeypatch: pytest.MonkeyPatch,
):
    from treebox.config import config_path, treebox_home

    marker = "~treebox-user-that-cannot-exist"
    monkeypatch.setenv("TREEBOX_HOME", f"{marker}/home")
    assert treebox_home() == Path(f"{marker}/home")

    monkeypatch.setenv("TREEBOX_CONFIG", f"{marker}/config.toml")
    assert config_path() == Path(f"{marker}/config.toml")


def test_unresolvable_user_config_is_clean_usage_error(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "TREEBOX_CONFIG",
        "~treebox-user-that-cannot-exist/config.toml",
    )

    res = _cli(["list", "--json"])

    assert res.exit_code == 2
    assert "Traceback" not in res.output
    assert json.loads(res.stderr)["error"]["code"] == "INVALID_CONFIG"


def test_default_missing_config_stays_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The default ~/.treebox/config.toml is a *search* path, not an assertion:
    # absent file there means built-in defaults, no error.
    from treebox.config import load_config

    monkeypatch.delenv("TREEBOX_CONFIG", raising=False)
    monkeypatch.setenv("TREEBOX_HOME", str(tmp_path / "nowhere"))
    cfg = load_config()
    assert cfg.isolation == "host" and cfg.harness == "claude"


def test_invalid_isolation_flag_is_clean_usage_error(no_user_config):
    # Flag overrides merge after load_config's file check, so a bogus
    # --isolation must be re-validated: exit 2, not a get_runner traceback.
    res = _cli(["create", "--isolation", "bogus"])
    assert res.exit_code == 2
    assert "Traceback" not in res.output
    assert "Invalid isolation" in res.output


def test_invalid_harness_flag_json_error_is_structured(no_user_config):
    res = _cli(["create", "--harness", "bogus", "--json"])
    assert res.exit_code == 2
    err = json.loads(res.stderr)
    assert err["schemaVersion"] == SCHEMA_VERSION
    assert err["error"]["code"] == "INVALID_CONFIG"
    assert "Invalid harness" in err["error"]["message"]


def test_create_invalid_name_json_error_is_structured(no_user_config):
    # NAME is validated before any repo access, so this is hermetic; the
    # structured error payload carries the code agents branch on.
    res = _cli(["create", "Bad_Name", "--json"])
    assert res.exit_code == 2
    err = json.loads(res.stderr)
    assert err["schemaVersion"] == SCHEMA_VERSION
    assert err["error"]["code"] == "INVALID_NAME"
    assert "hint" in err["error"]


def test_create_b_invalid_branch_json_error_is_structured(no_user_config):
    res = _cli(["create", "--checkout", "bad..name", "--json"])
    assert res.exit_code == 2
    err = json.loads(res.stderr)
    assert err["schemaVersion"] == SCHEMA_VERSION
    assert err["error"]["code"] == "INVALID_BRANCH"


def test_list_outside_repo_json_error_is_structured(tmp_path: Path, no_user_config):
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    res = _cli(["list", "--repo", str(not_a_repo), "--json"])
    assert res.exit_code == 2
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "NOT_A_REPO"
    assert "hint" in err["error"]


def test_enter_outside_repo_json_error_is_structured(tmp_path: Path, no_user_config):
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    res = _cli(["enter", "x", "--repo", str(not_a_repo), "--json"])
    assert res.exit_code == 2
    assert json.loads(res.stderr)["error"]["code"] == "NOT_A_REPO"


def test_doctor_json_exits_nonzero_on_hard_check_failure(tmp_path: Path, no_user_config):
    # SKILL.md promises "exits non-zero if a hard check fails" — the --json
    # path must agree with the human path instead of always returning 0.
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    res = _cli(["doctor", "--repo", str(not_a_repo), "--json"])
    assert res.exit_code == 1
    payload = json.loads(res.stdout)  # payload is still emitted before exiting
    assert payload["schemaVersion"] == SCHEMA_VERSION
    repo_check = next(c for c in payload["checks"] if c["name"] == "repo")
    assert repo_check["ok"] is False


def test_json_error_payload_is_pretty_printed_like_success(no_user_config):
    # Error payloads use the same serialization as success payloads (indent=2).
    res = _cli(["create", "bad name", "--json"])
    assert res.exit_code == 2
    assert res.stderr == json.dumps(json.loads(res.stderr), indent=2) + "\n"
    assert json.loads(res.stderr)["error"]["code"] == "INVALID_NAME"


def test_cli_startup_restores_default_sigpipe(no_user_config):
    # Piping `treebox ... --json | head -1` must end quietly (SIGPIPE default),
    # not spray BrokenPipeError noise: the app callback resets the handler
    # Python installs (SIG_IGN → BrokenPipeError) back to SIG_DFL on POSIX.
    import signal

    if not hasattr(signal, "SIGPIPE"):
        pytest.skip("POSIX-only behavior")
    previous = signal.getsignal(signal.SIGPIPE)
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)  # Python's startup default
        res = _cli(["version"])
        assert res.exit_code == 0
        assert signal.getsignal(signal.SIGPIPE) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGPIPE, previous)


def test_teardown_json_reports_what_was_removed(repo: Path, hermetic_config):
    root = str(repo.parent / "wts")
    res = _cli(["create", "tx", "--repo", str(repo), "--root", root, "--print"])
    assert res.exit_code == 0, res.output
    res = _cli(["teardown", "tx", "--repo", str(repo), "--root", root, "--force", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["schemaVersion"] == SCHEMA_VERSION
    (record,) = payload["worktrees"]
    assert record["name"] == "tx"
    assert record["branch"] == "tx"
    assert record["removed"] is True
    assert record["branch_deleted"] is False
    assert record["container"] == "cleaned"
    assert record["volumes_removed"] is False
    assert not (Path(root) / "tx").exists()


def test_teardown_json_already_gone_and_delete_branch(repo: Path, hermetic_config):
    root = str(repo.parent / "wts")
    _cli(["create", "ty", "--repo", str(repo), "--root", root, "--print"])
    import shutil

    shutil.rmtree(Path(root) / "ty")  # dir gone (still registered), branch remains
    res = _cli(
        [
            "teardown",
            "ty",
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
    assert record["removed"] is False  # already gone, still exit 0
    assert record["branch_deleted"] is True
    # With the dir gone the recorded isolation mode is unreadable, so container
    # teardown is skipped — never reported "cleaned" off a guessed mode.
    assert record["container"] == "skipped"


def test_teardown_pruned_worktree_by_exact_branch(repo: Path, hermetic_config):
    """When the worktree is gone AND pruned (nothing left to resolve), an exact
    local branch still tears down: prune + optional branch delete."""
    import shutil

    from treebox import git

    root = str(repo.parent / "wts")
    _cli(["create", "tz", "--repo", str(repo), "--root", root, "--print"])
    shutil.rmtree(Path(root) / "tz")
    git.worktree_prune(str(repo))

    res = _cli(
        [
            "teardown",
            "tz",
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
    assert record["removed"] is False and record["branch_deleted"] is True
    assert not git.local_branch_exists(str(repo), "tz")


def test_teardown_json_conflicts_are_structured(repo: Path, hermetic_config):
    root = str(repo.parent / "wts")
    _cli(["create", "tw", "--repo", str(repo), "--root", root, "--print"])
    # Drop the setup hook's untracked marker so the worktree starts clean.
    (Path(root) / "tw" / "setup.log").unlink()

    # Without --force teardown never prompts in --json mode: structured exit 5.
    res = _cli(["teardown", "tw", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "NEEDS_CONFIRMATION"
    assert res.stdout == ""

    # A dirty worktree is a structured DIRTY_WORKTREE conflict, with the path.
    (Path(root) / "tw" / "uncommitted.txt").write_text("x")
    res = _cli(["teardown", "tw", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 5
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "DIRTY_WORKTREE"
    assert err["error"]["path"] == str(Path(root) / "tw")


# --- teardown chooser: selection parsing --------------------------------------


def test_parse_selection_ranges_all_and_blank():
    from treebox.cli import _parse_selection

    assert _parse_selection("", 5) == []
    assert _parse_selection("  ", 5) == []
    assert _parse_selection("all", 3) == [0, 1, 2]
    assert _parse_selection("*", 3) == [0, 1, 2]
    # 1-based input -> 0-based indices, deduped and sorted.
    assert _parse_selection("1,3", 5) == [0, 2]
    assert _parse_selection("1-3", 5) == [0, 1, 2]
    assert _parse_selection("3 1  2", 5) == [0, 1, 2]
    assert _parse_selection("2,2,2", 5) == [1]
    # Out-of-range and junk tokens are dropped, never crash.
    assert _parse_selection("0,4,99,foo,-", 4) == [3]
    assert _parse_selection("2-10", 4) == [1, 2, 3]
    # Bounds are clamped BEFORE the range is built: an absurd typo costs at
    # most n iterations instead of hanging the CLI for ~10^11 of them.
    assert _parse_selection("1-99999999999", 3) == [0, 1, 2]
    assert _parse_selection("-99999999999--99999999998", 3) == []
    assert _parse_selection("99999999998-99999999999", 3) == []


# --- teardown chooser: status badge -------------------------------------------


def test_status_compute_local_signals():
    from treebox import status

    # Clean + merged-by-ancestor -> safe, green.
    s = status.compute(
        dirty=False, placeholder=False, has_upstream=True, ahead=0, merged_ancestor=True, pr=None
    )
    assert s.safe and s.style == "wt.ok" and "merged" in s.label

    # Dirty always wins the color and blocks safety, even when merged.
    s = status.compute(
        dirty=True, placeholder=False, has_upstream=True, ahead=0, merged_ancestor=True, pr=None
    )
    assert not s.safe and s.style == "wt.fail" and "uncommitted" in s.label

    # Placeholder / never-pushed -> caution, not safe.
    s = status.compute(
        dirty=False, placeholder=True, has_upstream=False, ahead=0, merged_ancestor=False, pr=None
    )
    assert not s.safe and s.style == "wt.warn" and "never pushed" in s.label

    # Ahead of upstream and unmerged.
    s = status.compute(
        dirty=False, placeholder=False, has_upstream=True, ahead=3, merged_ancestor=False, pr=None
    )
    assert not s.safe and "ahead 3" in s.label

    # Clean, pushed, but not merged.
    s = status.compute(
        dirty=False, placeholder=False, has_upstream=True, ahead=0, merged_ancestor=False, pr=None
    )
    assert not s.safe and s.label == "unmerged" and s.style == "wt.muted"


def test_status_compute_fresh_placeholder_reads_empty_not_merged():
    from treebox import status

    # A just-created worktree sits on its un-renamed placeholder at origin/<base>
    # with no commits, so the ancestor check trivially passes. That is *empty*,
    # not merged: safe to delete, but the badge must not imply a landed PR.
    s = status.compute(
        dirty=False, placeholder=True, has_upstream=False, ahead=0, merged_ancestor=True, pr=None
    )
    assert s.safe and s.style == "wt.muted"
    assert s.label == "⚠ empty" and "merged" not in s.label

    # Uncommitted work in an otherwise-empty tree: still shows empty, but the
    # dirty flag blocks safety so the work is not silently deletable.
    s = status.compute(
        dirty=True, placeholder=True, has_upstream=False, ahead=0, merged_ancestor=True, pr=None
    )
    assert not s.safe and s.style == "wt.fail" and "⚠ empty" in s.label

    # A named, pushed branch whose commits landed by ancestor is genuinely
    # merged, not empty — the empty carve-out only applies to never-pushed tips.
    s = status.compute(
        dirty=False, placeholder=False, has_upstream=True, ahead=0, merged_ancestor=True, pr=None
    )
    assert s.safe and s.style == "wt.ok" and "merged" in s.label


def test_status_compute_pr_overrides_squash_merge():
    from treebox import status
    from treebox.forge import PRStatus

    # A squash merge is invisible to the local ancestor check (merged_ancestor
    # False), but the forge PR state knows it's merged -> safe.
    s = status.compute(
        dirty=False,
        placeholder=False,
        has_upstream=True,
        ahead=2,
        merged_ancestor=False,
        pr=PRStatus(state="merged", number=42),
    )
    assert s.safe and "merged" in s.label and "#42" in s.label

    # An open PR shows as caution, not safe.
    s = status.compute(
        dirty=False,
        placeholder=False,
        has_upstream=True,
        ahead=1,
        merged_ancestor=False,
        pr=PRStatus(state="open", number=7),
    )
    assert not s.safe and s.style == "wt.warn" and "PR #7 open" in s.label


def test_status_compute_closed_pr_does_not_mask_local_merge():
    from treebox import status
    from treebox.forge import PRStatus

    # The branch's own PR was closed, but its commits already landed in
    # origin/<base> (merged via another PR / directly) -> still merged, safe.
    s = status.compute(
        dirty=False,
        placeholder=False,
        has_upstream=True,
        ahead=0,
        merged_ancestor=True,
        pr=PRStatus(state="closed", number=9),
    )
    assert s.safe and s.style == "wt.ok" and "merged" in s.label


# --- teardown chooser: forge routing & degradation ----------------------------


def test_forge_detect_routes_by_authed_cli(monkeypatch: pytest.MonkeyPatch):
    from treebox import forge, git

    monkeypatch.setattr(git, "origin_host", lambda repo: "github.com")
    # gh authed -> GhForge.
    monkeypatch.setattr(forge.GhForge, "authed", lambda self: True)
    monkeypatch.setattr(forge.GlabForge, "authed", lambda self: True)
    assert isinstance(forge.detect("/r"), forge.GhForge)

    # gh not authed, glab is -> falls through to GlabForge (self-hosted GitLab).
    monkeypatch.setattr(forge.GhForge, "authed", lambda self: False)
    assert isinstance(forge.detect("/r"), forge.GlabForge)

    # Neither authed -> None (Tier-1 only).
    monkeypatch.setattr(forge.GlabForge, "authed", lambda self: False)
    assert forge.detect("/r") is None

    # No attributable origin (Bitbucket via unknown host / local path) -> None.
    monkeypatch.setattr(git, "origin_host", lambda repo: None)
    assert forge.detect("/r") is None


def test_gh_pr_status_parses_and_maps_state(monkeypatch: pytest.MonkeyPatch):
    import subprocess

    from treebox import forge

    def fake_run(argv, **kw):
        payload = '{"state":"MERGED","number":42,"url":"http://x/42","isDraft":false}'
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    pr = forge.GhForge("github.com").pr_status("/r", "feat/x")
    assert pr is not None and pr.merged and pr.number == 42 and pr.url == "http://x/42"


def test_glab_pr_status_queries_all_states_and_prefers_merged(monkeypatch: pytest.MonkeyPatch):
    import subprocess

    from treebox import forge

    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(argv)
        # An abandoned closed MR listed before the one that actually merged.
        payload = (
            '[{"state":"closed","iid":7,"web_url":"http://x/7"},'
            '{"state":"merged","iid":9,"web_url":"http://x/9"}]'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    pr = forge.GlabForge("gitlab.com").pr_status("/r", "feat/x")
    # glab's default state filter is opened-only, which would hide merged MRs.
    assert "--all" in seen[0]
    assert pr is not None and pr.merged and pr.number == 9 and pr.url == "http://x/9"


def test_glab_pr_status_maps_states(monkeypatch: pytest.MonkeyPatch):
    import subprocess

    from treebox import forge

    def fake(payload: str):
        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

        return fake_run

    glab = forge.GlabForge("gitlab.com")
    monkeypatch.setattr(subprocess, "run", fake('[{"state":"opened","iid":1,"draft":true}]'))
    assert glab.pr_status("/r", "b").state == "draft"
    monkeypatch.setattr(subprocess, "run", fake('[{"state":"opened","iid":1}]'))
    assert glab.pr_status("/r", "b").state == "open"
    monkeypatch.setattr(subprocess, "run", fake('[{"state":"closed","iid":1}]'))
    assert glab.pr_status("/r", "b").state == "closed"
    monkeypatch.setattr(subprocess, "run", fake("[]"))
    assert glab.pr_status("/r", "b") is None


def test_gh_pr_status_maps_draft_and_open(monkeypatch: pytest.MonkeyPatch):
    import subprocess

    from treebox import forge

    def make(state, is_draft):
        def fake_run(argv, **kw):
            draft = "true" if is_draft else "false"
            payload = f'{{"state":"{state}","number":1,"url":"","isDraft":{draft}}}'
            return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

        return fake_run

    monkeypatch.setattr(subprocess, "run", make("OPEN", True))
    assert forge.GhForge("github.com").pr_status("/r", "b").state == "draft"
    monkeypatch.setattr(subprocess, "run", make("OPEN", False))
    assert forge.GhForge("github.com").pr_status("/r", "b").state == "open"


def test_forge_pr_status_degrades_on_failure(monkeypatch: pytest.MonkeyPatch):
    import subprocess

    from treebox import forge

    # CLI missing -> None (no crash).
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    assert forge.GhForge("github.com").pr_status("/r", "b") is None
    assert forge.GhForge("github.com").authed() is False

    # Non-zero exit -> None.
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom")
    )
    assert forge.GhForge("github.com").pr_status("/r", "b") is None

    # Zero exit but garbage JSON -> None.
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "not json", "")
    )
    assert forge.GhForge("github.com").pr_status("/r", "b") is None


# --- issue #131 §2: non-CLI gaps -------------------------------------------------


def test_get_runner_rejects_unknown_isolation():
    """Defense-in-depth behind validate_config: an isolation mode no runner
    claims is a loud error, never a silent default."""
    from treebox.runners import get_runner

    with pytest.raises(ValueError, match="bogus"):
        get_runner(Config(isolation="bogus"))


def test_registry_vocabularies_cannot_drift():
    """The registries are the single source of the CLI vocabularies: the
    VALID_* tuples derive from them, and config's Literal aliases (typing the
    internal seams) must name exactly the same sets."""
    from typing import get_args

    from treebox import config
    from treebox.harnesses import HARNESSES, VALID_HARNESSES
    from treebox.runners import RUNNERS, VALID_ISOLATION

    assert tuple(RUNNERS) == VALID_ISOLATION
    assert tuple(h.name for h in HARNESSES) == VALID_HARNESSES
    assert set(VALID_ISOLATION) == set(get_args(config.Isolation))
    assert set(VALID_HARNESSES) == set(get_args(config.Harness))


def test_runner_facts_pin_doctor_vocabulary():
    """Doctor's per-runner strings and login gate come from RunnerFacts: the
    host runner hard-requires a subscription login (the agent runs with the
    live host dirs); the docker runner stages copies, so a missing login is
    advisory."""
    from treebox.runners import DockerRunner, HostRunner

    host = HostRunner(Config()).facts()
    assert host.preflight_detail == "no container dependencies"
    assert host.login_required is True

    boxed = DockerRunner(Config(isolation="docker")).facts()
    assert boxed.preflight_detail == "docker daemon ok"
    assert boxed.login_required is False


def test_state_load_corrupt_json_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A corrupt state file reads as 'no state' (list shows deps=unknown),
    never a crash."""
    from treebox import git, state

    monkeypatch.setattr(git, "git_dir", lambda p: str(tmp_path))
    (tmp_path / "treebox-state.json").write_text("{not json")
    assert state.load(tmp_path) is None


def test_config_setup_hook_string_normalizes_to_list(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('setup_hook = "echo one"\n')
    assert load_config(cfg_file).setup_hook == ["echo one"]
    cfg_file.write_text('setup_hook = ["echo one", "echo two"]\n')
    assert load_config(cfg_file).setup_hook == ["echo one", "echo two"]


def test_git_subprocesses_ignore_the_users_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The autouse ``isolated_git_config`` fixture must hide the developer's
    global gitconfig from every git the suite spawns: a HOME whose .gitconfig
    would change init's default branch has no effect on a fresh init."""
    import subprocess

    out = subprocess.run(
        ["git", "--version"], check=True, stdout=subprocess.PIPE, text=True
    ).stdout.split()[-1]
    major, minor = (int(p) for p in out.split(".")[:2])
    if (major, minor) < (2, 32):
        pytest.skip("GIT_CONFIG_GLOBAL needs git >= 2.32")

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[init]\n\tdefaultBranch = gotcha\n")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "fresh"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert head != "refs/heads/gotcha"


@pytest.mark.parametrize(
    "bad", ["setup_hook = 42", "setup_hook = true", "setup_hook = 3.14", "setup_hook = [1, 2]"]
)
def test_config_wrong_typed_setup_hook_is_value_error(tmp_path: Path, bad: str):
    # Valid TOML with a wrong-typed setup_hook used to escape as a TypeError
    # (issue #139) — it must be the same clean ValueError every other invalid
    # config value raises, so the CLI maps it to INVALID_CONFIG / exit 2.
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(bad + "\n")
    with pytest.raises(ValueError, match="setup_hook must be a string or a list of strings"):
        load_config(cfg_file)


@pytest.mark.parametrize(
    ("bad", "key"),
    [
        ("base = 42", "base"),
        ('firewall = "yes"', "firewall"),
        ("firewall = 1", "firewall"),
        ("root = false", "root"),
        ("env_file = 3.14", "env_file"),
        ("template = 7", "template"),
        ("isolation = true", "isolation"),
        ("harness = 42", "harness"),
    ],
)
def test_config_wrong_typed_scalars_are_value_errors(tmp_path: Path, bad: str, key: str):
    # A wrong-typed scalar must be rejected loudly, not accepted and left to
    # misbehave downstream (`base = 42` reaching git, truthy `firewall = "yes"`).
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(bad + "\n")
    with pytest.raises(ValueError, match=f"{key} must be a"):
        load_config(cfg_file)


def test_config_wrong_typed_value_is_clean_invalid_config_in_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # End to end through the CLI: exit 2 + structured INVALID_CONFIG in --json
    # (no traceback, no missing payload), same as any other bad config.
    bad = tmp_path / "config.toml"
    bad.write_text("setup_hook = 42\n")
    monkeypatch.setenv("TREEBOX_CONFIG", str(bad))
    res = _cli(["list", "--json"])
    assert res.exit_code == 2
    err = json.loads(res.stderr)
    assert err["error"]["code"] == "INVALID_CONFIG"
    assert "setup_hook" in err["error"]["message"]
    assert "hint" in err["error"]

    res = _cli(["list"])
    assert res.exit_code == 2
    assert "Traceback" not in res.output and "TypeError" not in res.output
    assert "setup_hook" in res.output


def test_config_caches_table_merges_over_defaults(tmp_path: Path):
    """A [caches] entry overrides that ecosystem's default store without
    dropping the other ecosystems' defaults."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[caches]\nuv = "/custom/uv"\n')
    cfg = load_config(cfg_file)
    assert cfg.caches["uv"] == "/custom/uv"
    defaults = {eco.cache_key for eco in ecosystems.ECOSYSTEMS if eco.cache_key}
    assert defaults <= set(cfg.caches)  # everything else still wired


# --- git: the silent fetch credential cascade -------------------------------------


def _no_ambient_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cascade must behave the same on a dev box with ambient git auth env."""
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)


def test_fetch_origin_ambient_success_is_one_silent_call(monkeypatch: pytest.MonkeyPatch):
    import subprocess

    from treebox import git

    _no_ambient_creds(monkeypatch)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)
    assert git.fetch_origin("/r", required=True) is True

    ((argv, kw),) = calls  # ambient success: exactly one attempt, no rewrite probe
    assert "fetch" in argv
    assert argv[argv.index("--") + 1] == "origin"
    # Strictly non-interactive: no terminal prompts, ssh in BatchMode.
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in kw["env"]["GIT_SSH_COMMAND"]


def test_fetch_origin_falls_back_to_https_with_host_cli_token(monkeypatch: pytest.MonkeyPatch):
    """Ambient fails (SSH remote, no agent) → the fetch retries over HTTPS with
    the authed host CLI wired as git's credential helper for exactly this call:
    the inherited helper cleared first, then the CLI helper set."""
    import subprocess

    from treebox import git

    _no_ambient_creds(monkeypatch)
    monkeypatch.setattr(
        git, "_origin_https", lambda repo: ("github.com", "https://github.com/o/r.git")
    )
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/gh" if b == "gh" else None)
    fetches: list[list[str]] = []

    def fake_run(argv, **kw):
        argv = list(argv)
        if argv[0] == "gh":  # the `gh auth token --hostname github.com` probe
            assert argv[-1] == "github.com"
            return subprocess.CompletedProcess(argv, 0, "", "")
        fetches.append(argv)
        failed = argv[argv.index("--") + 1] == "origin"
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)
    assert git.fetch_origin("/r", required=True) is True

    ambient, https = fetches
    assert ambient[ambient.index("--") + 1] == "origin"
    assert https[https.index("--") + 1] == "https://github.com/o/r.git"
    assert https[-1] == "+refs/heads/*:refs/remotes/origin/*"  # mapped back onto origin
    clear = https.index("credential.https://github.com.helper=")
    wire = https.index("credential.https://github.com.helper=!gh auth git-credential")
    assert clear < wire  # clear-then-set, scoped to this invocation


def test_fetch_origin_all_silent_paths_fail(monkeypatch: pytest.MonkeyPatch):
    """Every silent path failing surfaces the captured git output in the
    FetchError when required, and returns False when not."""
    import subprocess

    from treebox import git

    _no_ambient_creds(monkeypatch)
    monkeypatch.setattr(git, "_origin_https", lambda repo: None)  # no HTTPS rewrite possible

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "Permission denied (publickey)", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)
    with pytest.raises(git.FetchError, match="publickey"):
        git.fetch_origin("/r", required=True, interactive=False)
    assert git.fetch_origin("/r", required=False, interactive=False) is False


def test_fetch_origin_interactive_prompt_is_last_resort(monkeypatch: pytest.MonkeyPatch):
    """The terminal-attached fetch only runs after every silent path failed —
    a working silent credential must never bother the user."""
    import subprocess

    from treebox import git

    _no_ambient_creds(monkeypatch)
    monkeypatch.setattr(git, "_origin_https", lambda repo: None)
    order: list[str] = []

    def fake_run(argv, **kw):
        silent = "stdout" in kw  # _silent_fetch captures; _fetch_prompt inherits stdio
        order.append("silent" if silent else "prompt")
        return subprocess.CompletedProcess(argv, 1 if silent else 0, "", "")

    monkeypatch.setattr(git.subprocess, "run", fake_run)
    assert git.fetch_origin("/r", required=True, interactive=True) is True
    assert order == ["silent", "prompt"]


def test_origin_reachable_probes_the_same_cascade(monkeypatch: pytest.MonkeyPatch):
    """doctor's reachability verdict runs ls-remote over the same silent
    attempts as the real fetch: None without origin, True/False otherwise."""
    import subprocess

    from treebox import git

    _no_ambient_creds(monkeypatch)
    monkeypatch.setattr(git, "_origin_https", lambda repo: None)

    monkeypatch.setattr(git, "has_origin", lambda repo: False)
    assert git.origin_reachable("/r") is None

    monkeypatch.setattr(git, "has_origin", lambda repo: True)
    seen: list[list[str]] = []

    def fake_run(rc):
        def run(argv, **kw):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, rc, "", "")

        return run

    monkeypatch.setattr(git.subprocess, "run", fake_run(0))
    assert git.origin_reachable("/r") is True
    assert "ls-remote" in seen[-1]

    monkeypatch.setattr(git.subprocess, "run", fake_run(1))
    assert git.origin_reachable("/r") is False


def test_cred_config_for_prefers_the_authed_cli(monkeypatch: pytest.MonkeyPatch):
    import subprocess

    from treebox import git

    # glab installed and authed for the host, gh absent → glab helper wired.
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/glab" if b == "glab" else None)
    monkeypatch.setattr(
        git.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
    )
    flags = git._cred_config_for("gitlab.example.com")
    assert flags[1] == "credential.https://gitlab.example.com.helper="
    assert flags[3] == "credential.https://gitlab.example.com.helper=!glab auth git-credential"

    # No host CLI installed → no flags; git's own helpers still apply.
    monkeypatch.setattr("shutil.which", lambda b: None)
    assert git._cred_config_for("github.com") == []


# --- host runner: setup / launch ---------------------------------------------------


class _HostRecorder:
    """Reporter stand-in for the host runner: records steps/notes/warnings and
    can fail chosen step labels to exercise the non-fatal-setup contract."""

    def __init__(self, fail_labels: set[str] | None = None) -> None:
        self.steps: list[tuple[str, list[str], str | None, dict[str, str]]] = []
        self.notes: list[tuple[str, str]] = []
        self.warnings: list[str] = []
        self._fail = fail_labels or set()

    def step(self, label, detail, argv, *, cwd=None, env=None):
        self.steps.append((label, list(argv), cwd, dict(env or {})))
        if label in self._fail:
            from treebox.output import StepError

            raise StepError(label, 1, "boom")
        return ""

    def note(self, label, detail=""):
        self.notes.append((label, detail))

    def warn(self, msg):
        self.warnings.append(msg)


def _host_worktree(tmp_path: Path) -> Worktree:
    wt_path = tmp_path / "wt"
    wt_path.mkdir(exist_ok=True)
    return Worktree("/repo", "wt", "treebox/wt", "main", wt_path)


def test_host_auto_setup_runs_ecosystem_steps_with_cache_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from treebox.runners import host as host_mod

    wt = _host_worktree(tmp_path)
    (wt.path / "uv.lock").write_text("")
    monkeypatch.setattr(host_mod, "have", lambda c: True)
    rec = _HostRecorder()

    host_mod.HostRunner(Config(caches={"uv": str(tmp_path / "uvc")})).setup(
        wt, cold=False, reporter=rec
    )

    ((label, argv, cwd, env),) = rec.steps
    assert label == "setup · uv"
    assert argv == ["uv", "sync"]
    assert cwd == str(wt.path)
    assert env["UV_CACHE_DIR"] == str(tmp_path / "uvc")  # shared warm cache wired


def test_host_auto_setup_skips_missing_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from treebox.runners import host as host_mod

    wt = _host_worktree(tmp_path)
    (wt.path / "uv.lock").write_text("")
    monkeypatch.setattr(host_mod, "have", lambda c: False)
    rec = _HostRecorder()

    host_mod.HostRunner(Config()).setup(wt, cold=False, reporter=rec)

    assert rec.steps == []  # nothing executed
    assert ("setup · uv", "uv not found; skipped") in rec.notes


def test_host_auto_setup_failure_is_nonfatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from treebox.runners import host as host_mod

    wt = _host_worktree(tmp_path)
    (wt.path / "uv.lock").write_text("")
    monkeypatch.setattr(host_mod, "have", lambda c: True)
    rec = _HostRecorder(fail_labels={"setup · uv"})

    host_mod.HostRunner(Config()).setup(wt, cold=False, reporter=rec)  # must not raise

    assert any("uv setup failed; continuing" in w for w in rec.warnings)


def test_host_auto_setup_notes_when_no_manifests(tmp_path: Path):
    from treebox.runners import host as host_mod

    rec = _HostRecorder()
    host_mod.HostRunner(Config()).setup(_host_worktree(tmp_path), cold=False, reporter=rec)
    assert ("setup", "no package manifests") in rec.notes


def test_host_cold_setup_uses_throwaway_cache_and_cleans_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from treebox.runners import host as host_mod

    wt = _host_worktree(tmp_path)
    (wt.path / "uv.lock").write_text("")
    monkeypatch.setattr(host_mod, "have", lambda c: True)
    rec = _HostRecorder()

    host_mod.HostRunner(Config(caches={"uv": str(tmp_path / "shared")})).setup(
        wt, cold=True, reporter=rec
    )

    ((_, _, _, env),) = rec.steps
    cold_cache = env["UV_CACHE_DIR"]
    assert "treebox-cold-" in cold_cache  # redirected away from the shared store
    assert cold_cache != str(tmp_path / "shared")
    assert not Path(cold_cache).parent.exists()  # throwaway removed after setup


def test_host_override_hook_failure_warns_and_continues(tmp_path: Path):
    from treebox.runners import host as host_mod

    # cache_env creates the configured cache dir, so it must live under tmp_path.
    shared_cache = str(tmp_path / "shared" / "uv")
    rec = _HostRecorder(fail_labels={"setup · hook 1"})
    cfg = Config(setup_hook=["exit 1", "echo ok"], caches={"uv": shared_cache})
    host_mod.HostRunner(cfg).setup(_host_worktree(tmp_path), cold=False, reporter=rec)

    labels = [label for (label, _, _, _) in rec.steps]
    assert labels == ["setup · hook 1", "setup · hook 2"]  # second hook still ran
    assert any("hook step 1 failed" in w for w in rec.warnings)
    # Custom hooks get the shared cache env too.
    assert rec.steps[0][3]["UV_CACHE_DIR"] == shared_cache
    assert rec.steps[0][1] == ["sh", "-c", "exit 1"]


def test_host_launch_runs_in_the_worktree_and_returns_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import os

    from treebox.harnesses import get_harness
    from treebox.runners.host import HostRunner

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\npwd > launched.txt\nexit 5\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    wt = _host_worktree(tmp_path)
    code = HostRunner(Config()).launch(wt, harness=get_harness("claude"), args=[])
    assert code == 5  # the agent's exit code, verbatim
    launched = (wt.path / "launched.txt").read_text().strip()
    assert Path(launched).resolve() == wt.path.resolve()  # ran in the box


def test_host_launch_missing_harness_is_instructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from treebox.harnesses import get_harness
    from treebox.runners import host as host_mod

    monkeypatch.setattr(host_mod, "have", lambda c: False)
    with pytest.raises(RuntimeError, match=r"not found on PATH.*writes ~/\.claude"):
        host_mod.HostRunner(Config()).launch(
            _host_worktree(tmp_path), harness=get_harness("claude"), args=[]
        )


# --- output: degradation matrix & step contract ------------------------------------


def _stderr_reporter(**kw):
    import io

    from treebox.output import Reporter

    buf = io.StringIO()
    return Reporter(stream=buf, **kw), buf


def test_reporter_quiet_suppresses_progress_but_not_problems():
    r, buf = _stderr_reporter(quiet=True)
    r.heading("create", "x")
    r.summary("k", "v")
    r.ok("step")
    r.note("step")
    r.info("fyi")
    r.blank()
    r.ready("1.0s", "claude")
    assert buf.getvalue() == ""  # progress fully suppressed
    r.warn("careful")
    r.error("broken")
    r.fail("step")
    r.hint("do this")
    out = buf.getvalue()
    assert "careful" in out and "broken" in out and "do this" in out and "✗" in out


def test_reporter_silent_suppresses_everything():
    r, buf = _stderr_reporter(silent=True)
    r.heading("create", "x")
    r.warn("careful")
    r.error("broken")
    r.fail("step")
    r.hint("do this")
    assert buf.getvalue() == ""  # --json mode: nothing but the payload, ever


def test_timing_column_renders_on_terminal_only():
    import io

    from rich.console import Console

    from treebox.output import THEME, Reporter

    r, buf = _stderr_reporter()
    r.ok("label", "detail", timing="1.2s")
    assert "1.2s" not in buf.getvalue()  # off-TTY: no padded ANSI in a pipe

    buf2 = io.StringIO()
    r2 = Reporter()
    r2.console = Console(
        file=buf2, force_terminal=True, color_system=None, width=80, theme=THEME, highlight=False
    )
    r2.ok("label", "detail", timing="1.2s")
    assert buf2.getvalue().rstrip("\n").endswith("1.2s")  # right-aligned timing


def test_step_failure_raises_and_dumps_the_captured_log():
    from treebox.output import StepError

    r, buf = _stderr_reporter()
    with pytest.raises(StepError) as exc_info:
        r.step("deps", "done", ["sh", "-c", "echo boom; exit 3"])
    assert exc_info.value.returncode == 3
    assert "boom" in exc_info.value.log
    out = buf.getvalue()
    assert "✗" in out and "output" in out and "boom" in out  # framed log shown


def test_step_success_hides_output_and_returns_log():
    r, buf = _stderr_reporter()
    log = r.step("deps", "done", ["sh", "-c", "echo hi"])
    assert "hi" in log
    out = buf.getvalue()
    assert "✓" in out and "deps" in out
    assert "hi" not in out  # success output stays hidden


def test_step_verbose_streams_instead_of_capturing():
    from treebox.output import StepError

    r, buf = _stderr_reporter(verbose=True)
    assert r.step("deps", "done", ["sh", "-c", "true"]) == ""  # nothing captured
    assert "running" in buf.getvalue()
    with pytest.raises(StepError) as exc_info:
        r.step("deps", "done", ["sh", "-c", "exit 2"])
    assert exc_info.value.returncode == 2 and exc_info.value.log == ""


def test_task_resolves_to_ok_or_fail():
    r, buf = _stderr_reporter()
    with r.task("prep", "done"):
        pass
    assert "✓" in buf.getvalue()

    r2, buf2 = _stderr_reporter()
    with pytest.raises(ValueError), r2.task("prep"):
        raise ValueError("inner")
    assert "✗" in buf2.getvalue()  # failure row rendered, exception propagated


def test_render_list_empty_points_at_create():
    import io

    from rich.console import Console

    from treebox.output import THEME, Reporter

    buf = io.StringIO()
    r = Reporter()
    r.data_console = Console(file=buf, width=100, theme=THEME, highlight=False, color_system=None)
    r.render_list([], "/repo")
    out = buf.getvalue()
    assert "no worktrees yet" in out and "treebox create" in out


def test_render_doctor_runs_slow_checks_and_collects_advisories():
    import io

    from rich.console import Console

    from treebox.output import THEME, DoctorCheck, Reporter

    buf = io.StringIO()
    r = Reporter()
    r.data_console = Console(file=buf, width=100, theme=THEME, highlight=False, color_system=None)

    cheap = [DoctorCheck("git", True, "2.43.0")]
    slow = [
        (
            "checking git auth",
            lambda: DoctorCheck("git auth", False, "no credential", "run gh auth login"),
        )
    ]
    checks, advisories = r.render_doctor(cheap, slow, "host", width=10)

    assert checks == [
        DoctorCheck("git", True, "2.43.0"),
        DoctorCheck("git auth", False, "no credential", "run gh auth login"),
    ]
    assert advisories == ["run gh auth login"]
    out = buf.getvalue()
    assert "doctor" in out and "✓ git" in out and "✗ git auth" in out


def test_render_doctor_marks_missing_optional_env_as_note():
    import io

    from rich.console import Console

    from treebox.output import THEME, DoctorCheck, Reporter

    buf = io.StringIO()
    r = Reporter()
    r.data_console = Console(file=buf, width=100, theme=THEME, highlight=False, color_system=None)

    checks, advisories = r.render_doctor(
        [DoctorCheck(".env", False, "/repo/.env", required=False)], [], "host", width=4
    )

    assert checks == [DoctorCheck(".env", False, "/repo/.env", required=False)]
    assert advisories == []
    out = buf.getvalue()
    assert "· .env" in out
    assert "/repo/.env · optional" in out
    assert "✗ .env" not in out


def test_render_doctor_verdict_branches():
    import io

    from rich.console import Console

    from treebox.output import THEME, Reporter

    def verdict(**kw) -> str:
        buf = io.StringIO()
        r = Reporter()
        r.data_console = Console(
            file=buf, width=100, theme=THEME, highlight=False, color_system=None
        )
        r.render_doctor_verdict(**kw)
        return buf.getvalue()

    assert "blocked: repo" in verdict(problems=["repo"], has_login=True)
    assert "usable, but" in verdict(problems=[], has_login=True, advisories=["a"])
    assert "no subscription login" in verdict(problems=[], has_login=False)
    assert "everything looks good" in verdict(problems=[], has_login=True)


# --- teardown pickers: wiring around questionary -----------------------------------


def _picker_entries():
    from treebox import status
    from treebox.cli import _PickerEntry
    from treebox.resolve import Candidate

    entries = []
    for name in ("alpha", "beta"):
        cand = Candidate(name=name, branch=f"treebox/{name}", path=f"/wts/{name}")
        st = status.WorktreeStatus(label="unmerged", style="wt.muted", safe=False)
        entries.append(_PickerEntry(cand, f"{name}  row", st))
    return entries


class _CannedAsk:
    def __init__(self, answer) -> None:
        self.answer = answer

    def ask(self):
        return self.answer


def test_prompt_selection_wires_questionary_choices(monkeypatch: pytest.MonkeyPatch):
    import questionary

    from treebox.cli import _prompt_selection

    entries = _picker_entries()
    captured: dict = {}

    def fake_checkbox(message, choices):
        captured["message"] = message
        captured["choices"] = choices
        return _CannedAsk([entries[0][0]])

    monkeypatch.setattr(questionary, "checkbox", fake_checkbox)
    r, _ = _stderr_reporter()
    assert _prompt_selection(r, entries) == [entries[0][0]]
    # A leading separator, then one Choice per worktree carrying the Candidate.
    assert isinstance(captured["choices"][0], questionary.Separator)
    assert [c.value for c in captured["choices"][1:]] == [e[0] for e in entries]

    # Ctrl+C (.ask() -> None) means "nothing picked".
    monkeypatch.setattr(questionary, "checkbox", lambda *a, **k: _CannedAsk(None))
    assert _prompt_selection(r, entries) == []


def test_choose_branches_to_delete_wiring(monkeypatch: pytest.MonkeyPatch):
    import questionary

    from treebox.cli import _choose_branches_to_delete

    entries = _picker_entries()
    chosen = [entries[0][0]]  # only alpha was picked for teardown
    captured: dict = {}

    def fake_checkbox(message, choices):
        captured["choices"] = choices
        return _CannedAsk([entries[0][0].path])

    monkeypatch.setattr(questionary, "checkbox", fake_checkbox)
    r, _ = _stderr_reporter()
    got = _choose_branches_to_delete(r, entries, chosen, default=True)
    assert got == {"/wts/alpha"}
    # Only the chosen subset is offered, checked per the --delete-branch default.
    offered = captured["choices"][1:]
    assert [c.value for c in offered] == ["/wts/alpha"]
    assert all(c.checked for c in offered)

    # Ctrl+C aborts the whole teardown (None); enter-with-none deletes nothing.
    monkeypatch.setattr(questionary, "checkbox", lambda *a, **k: _CannedAsk(None))
    assert _choose_branches_to_delete(r, entries, chosen, default=False) is None
    monkeypatch.setattr(questionary, "checkbox", lambda *a, **k: _CannedAsk([]))
    assert _choose_branches_to_delete(r, entries, chosen, default=False) == set()


def test_pickers_degrade_without_questionary(monkeypatch: pytest.MonkeyPatch):
    """The optional TUI dep being absent must keep teardown working: selection
    falls back to the numbered prompt, and the branch question keeps every
    branch unless --delete-branch said otherwise."""
    import sys

    import typer

    from treebox.cli import _choose_branches_to_delete, _prompt_selection

    monkeypatch.setitem(sys.modules, "questionary", None)  # import -> ImportError
    entries = _picker_entries()
    r, buf = _stderr_reporter()

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "2")
    assert _prompt_selection(r, entries) == [entries[1][0]]
    assert "alpha" in buf.getvalue()  # the numbered menu rendered

    chosen = [c for (c, _p, _s) in entries]
    assert _choose_branches_to_delete(r, entries, chosen, default=True) == {
        "/wts/alpha",
        "/wts/beta",
    }
    assert _choose_branches_to_delete(r, entries, chosen, default=False) == set()


def test_prompt_numbered_returns_picked_candidates(monkeypatch: pytest.MonkeyPatch):
    import typer

    from treebox.cli import _prompt_numbered

    entries = _picker_entries()
    r, buf = _stderr_reporter()
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "1,2")
    assert _prompt_numbered(r, entries) == [entries[0][0], entries[1][0]]
    out = buf.getvalue()
    assert "Select worktrees to tear down" in out
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "")
    assert _prompt_numbered(r, entries) == []  # blank cancels


def test_resolve_version_falls_back_to_package_constant(monkeypatch: pytest.MonkeyPatch):
    """Version resolution survives an uninstalled distribution (bare source
    tree): the package constant backs the metadata lookup."""
    import importlib.metadata

    import treebox
    from treebox.cli import _resolve_version

    assert _resolve_version()  # normal path: the installed dist version

    def boom(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", boom)
    assert _resolve_version() == treebox.__version__


def test_host_dry_run_setup_lists_ecosystem_commands(tmp_path: Path):
    """--dry-run's setup plan: detected from the SOURCE repo's manifests via
    wt.repo (the worktree doesn't exist yet), or an explicit no-op comment."""
    from treebox.runners.host import HostRunner

    repo = tmp_path / "srcrepo"
    repo.mkdir()
    (repo / "uv.lock").write_text("")
    wt = Worktree(str(repo), "wt", "treebox/wt", "main", tmp_path / "wts" / "wt")
    assert HostRunner(Config()).dry_run_setup(wt) == ["uv sync"]

    empty = tmp_path / "bare"
    empty.mkdir()
    bare_wt = Worktree(str(empty), "wt", "treebox/wt", "main", tmp_path / "wts" / "wt")
    (plan,) = HostRunner(Config()).dry_run_setup(bare_wt)
    assert plan.startswith("#") and "no-op" in plan

    hooked = HostRunner(Config(setup_hook=["echo hi"]))
    assert hooked.dry_run_setup(wt) == ["sh -c 'echo hi'"]


def test_worktree_status_missing_row_never_shells_out():
    """A missing (git-prunable) row gets the registration-only badge without
    touching the path — it may not exist to shell into."""
    from treebox.cli import _worktree_status

    st = _worktree_status("/repo", {"missing": True}, provider=None, default_base="main")
    assert st.safe is True  # just a stale registration: removing it loses nothing
    assert "registration only" in st.label


def test_parse_selection_skips_empty_tokens():
    from treebox.cli import _parse_selection

    assert _parse_selection(",1,,2,", 5) == [0, 1]


def test_worktree_list_parses_prunable(monkeypatch: pytest.MonkeyPatch):
    """A registration whose working dir is gone is flagged prunable, so callers
    can render it without shelling into a path that no longer exists."""
    from treebox import git

    porcelain = (
        "worktree /r\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /r/.wt/live\nHEAD def\nbranch refs/heads/treebox/live\n\n"
        "worktree /r/.wt/gone\nHEAD 000\nbranch refs/heads/treebox/gone\n"
        "prunable gitdir file points to non-existent location\n\n"
    )
    monkeypatch.setattr(git, "_run", lambda args, **kw: porcelain)
    recs = git.worktree_list("/r")
    assert [(Path(r.path).name, r.prunable) for r in recs] == [
        ("r", False),
        ("live", False),
        ("gone", True),
    ]


# --- assets: template enumeration --------------------------------------------


def test_available_templates_enumerates_default_plus_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import treebox.assets as assets

    monkeypatch.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
    monkeypatch.setenv("TREEBOX_HOME", str(tmp_path / "home"))

    # With no user templates, only the built-in default is selectable.
    assert assets.available_templates() == ["default"]

    (tmp_path / "home" / "templates" / "node").mkdir(parents=True)
    (tmp_path / "home" / "templates" / "py").mkdir(parents=True)
    (tmp_path / "home" / "templates" / "note.txt").write_text("not a dir")
    # Named user dirs join the default, sorted; stray files are ignored.
    assert assets.available_templates() == ["default", "node", "py"]


def test_missing_template_files_reports_gaps_in_manifest_order(tmp_path: Path):
    import treebox.assets as assets

    empty = tmp_path / "empty"
    empty.mkdir()
    assert assets.missing_template_files(empty) == list(assets.TEMPLATE_FILES)

    complete = assets.template_dir("default")
    assert assets.missing_template_files(complete) == []


# --- cli: template subcommand ------------------------------------------------


@pytest.fixture
def template_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate template resolution: $TREEBOX_HOME points at a scratch dir with no
    config, and no explicit $TREEBOX_TEMPLATE_DIR leaks in from the dev's env."""
    home = tmp_path / "home"
    monkeypatch.setenv("TREEBOX_HOME", str(home))
    monkeypatch.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
    cfg = tmp_path / "empty-config.toml"
    cfg.write_text("")
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    return home


def test_template_init_scaffolds_full_required_file_set(template_home: Path):
    import treebox.assets as assets

    res = _cli(["template", "init", "webdev"])
    assert res.exit_code == 0
    dest = template_home / "templates" / "webdev"
    # Every file the docker runner requires is present, so a later `create`
    # never throws "<file> not found in template dir" — the whole point of
    # scaffolding instead of hand-copying.
    assert assets.missing_template_files(dest) == []
    assert (dest / "firewall.json").is_file()  # extras copied too, not just the required set


def test_template_init_json_reports_validity(template_home: Path):
    res = _cli(["template", "init", "webdev", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["schemaVersion"] == SCHEMA_VERSION
    assert payload["template"]["name"] == "webdev"
    assert payload["template"]["valid"] is True
    assert payload["template"]["missing"] == []


def test_template_init_refuses_overwrite_without_force(template_home: Path):
    assert _cli(["template", "init", "webdev"]).exit_code == 0
    # A second init is a conflict (exit 5), never a silent clobber.
    clash = _cli(["template", "init", "webdev", "--json"])
    assert clash.exit_code == 5
    assert json.loads(clash.stderr)["error"]["code"] == "TEMPLATE_EXISTS"
    # --force overwrites in place.
    assert _cli(["template", "init", "webdev", "--force"]).exit_code == 0


def test_template_init_rejects_unsafe_name(template_home: Path):
    # A name with a path separator could escape the templates root — rejected.
    res = _cli(["template", "init", "bad/name", "--json"])
    assert res.exit_code == 2
    assert json.loads(res.stderr)["error"]["code"] == "INVALID_NAME"


def test_template_init_unknown_from_is_not_found(template_home: Path):
    res = _cli(["template", "init", "x", "--from", "nope", "--json"])
    assert res.exit_code == 3
    assert json.loads(res.stderr)["error"]["code"] == "TEMPLATE_NOT_FOUND"


def test_template_init_unknown_from_is_not_found_under_env_override(
    template_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # $TREEBOX_TEMPLATE_DIR wins for any name in template_dir, but init's
    # explicit --from is a named source: an unknown name must still be a loud
    # TEMPLATE_NOT_FOUND, never a silent copy of the override dir.
    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv("TREEBOX_TEMPLATE_DIR", str(override))
    res = _cli(["template", "init", "x", "--from", "nope", "--json"])
    assert res.exit_code == 3
    assert json.loads(res.stderr)["error"]["code"] == "TEMPLATE_NOT_FOUND"


def test_template_init_overwrite_removal_failure_has_distinct_code(
    template_home: Path, monkeypatch: pytest.MonkeyPatch
):
    # A failed --force cleanup is not the source==destination TEMPLATE_CONFLICT;
    # an agent must be able to tell "copy onto itself" from "removal failed".
    import treebox.cli as cli_mod

    assert _cli(["template", "init", "webdev"]).exit_code == 0

    def boom(_path: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(cli_mod.shutil, "rmtree", boom)
    res = _cli(["template", "init", "webdev", "--force", "--json"])
    assert res.exit_code == 1
    assert json.loads(res.stderr)["error"]["code"] == "OVERWRITE_FAILED"


def test_template_init_copy_failure_is_structured_and_preserves_existing(
    template_home: Path, monkeypatch: pytest.MonkeyPatch
):
    # A copytree failure must be a clean COPY_FAILED (not a bare traceback), and
    # under --force the pre-existing template must survive because the copy is
    # staged and swapped in only on success.
    import treebox.assets as assets
    import treebox.cli as cli_mod

    assert _cli(["template", "init", "webdev"]).exit_code == 0
    dest = template_home / "templates" / "webdev"
    before = sorted(p.name for p in dest.iterdir())

    def boom(_src: Path, _dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli_mod.shutil, "copytree", boom)
    res = _cli(["template", "init", "webdev", "--force", "--json"])
    assert res.exit_code == 1
    assert json.loads(res.stderr)["error"]["code"] == "COPY_FAILED"
    # The existing template is intact, and no staging residue is left behind.
    assert dest.is_dir()
    assert sorted(p.name for p in dest.iterdir()) == before
    assert assets.missing_template_files(dest) == []
    assert not (dest.parent / ".webdev.treebox-tmp").exists()


def test_template_path_resolves_default_and_named(template_home: Path):
    import treebox.assets as assets

    default = _cli(["template", "path"])
    assert default.exit_code == 0
    assert default.stdout.strip() == str(assets.template_dir("default"))

    _cli(["template", "init", "webdev"])
    named = _cli(["template", "path", "webdev"])
    assert named.stdout.strip() == str(template_home / "templates" / "webdev")

    # An unknown name is not-found (exit 3), not a silent fall back to default.
    assert _cli(["template", "path", "ghost"]).exit_code == 3


def test_create_missing_template_is_not_found_and_leaves_no_debris(
    repo: Path, tmp_path: Path, template_home: Path
):
    # A bad --template on the provisioning path is the same user error the
    # template sub-app classifies as not-found: exit 3 + TEMPLATE_NOT_FOUND,
    # never the generic catch-all (issue #20) - and it fails before any git
    # state exists, so no worktree or branch is left behind.
    from treebox import git as git_mod

    root = tmp_path / "wts"
    res = _cli(
        [
            "create",
            "t1",
            "--repo",
            str(repo),
            "--root",
            str(root),
            "--isolation",
            "docker",
            "--template",
            "nope",
            "--no-fetch",
            "--json",
        ]
    )
    assert res.exit_code == 3
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "TEMPLATE_NOT_FOUND"
    assert "treebox template init nope" in err["hint"]
    assert not git_mod.local_branch_exists(str(repo), "t1")
    assert not (root / "t1").exists()


def test_classify_template_not_found_matches_template_subapp():
    # The provisioning classifier and the template sub-app must agree on what
    # a missing template means - enter with a deleted recorded template routes
    # through _classify at container-render time.
    import treebox.assets as assets
    from treebox.cli import EXIT_NOTFOUND, _classify

    info = _classify(assets.TemplateNotFoundError("nope", "No template named 'nope'."))
    assert info.exit_code == EXIT_NOTFOUND
    assert info.error_code == "TEMPLATE_NOT_FOUND"
    assert info.hint is not None and "treebox template init nope" in info.hint


def test_template_list_json_marks_default_and_flags_broken(template_home: Path):
    # A dir missing required files is surfaced as invalid, not hidden — so a
    # broken template is caught here rather than mid-`create`.
    broken = template_home / "templates" / "broken"
    broken.mkdir(parents=True)
    (broken / "container.json").write_text("{}")

    res = _cli(["template", "list", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    by_name = {t["name"]: t for t in payload["templates"]}
    assert by_name["default"]["default"] is True
    assert by_name["default"]["valid"] is True
    assert by_name["broken"]["valid"] is False
    assert "Dockerfile" in by_name["broken"]["missing"]


def test_template_list_human_view_shows_default_highlights(template_home: Path):
    # The human view highlights the bundled default's main blocks so an operator
    # sees what the stock sandbox ships without reading the Dockerfile.
    res = _cli(["template", "list"])
    assert res.exit_code == 0
    assert "default" in res.stdout
    assert "Bundled 'default' template includes" in res.stdout
    # A representative sample of the curated highlight list.
    for needle in ("Python", "Playwright", "GitHub CLI"):
        assert needle in res.stdout


def test_default_template_highlights_match_bundled_dockerfile(template_home: Path):
    # The curated highlight copy hand-duplicates version facts from the bundled
    # Dockerfile; this pins them together so a version bump there can't silently
    # stale the user-facing list.
    import treebox.assets as assets

    dockerfile = (assets.template_dir("default") / "Dockerfile").read_text()
    highlights = "\n".join(assets.DEFAULT_TEMPLATE_HIGHLIGHTS)

    python_version = re.search(r"Python (\d+\.\d+)", highlights)
    assert python_version is not None
    assert f"python:{python_version.group(1)}" in dockerfile

    node_major = re.search(r"Node\.js (\d+)", highlights)
    assert node_major is not None
    assert f"NODE_MAJOR={node_major.group(1)}" in dockerfile


def test_template_ls_is_alias_for_list(template_home: Path):
    # `ls` is muscle-memory parity with the top-level list alias.
    listed = _cli(["template", "list"])
    aliased = _cli(["template", "ls"])
    assert aliased.exit_code == 0
    assert aliased.stdout == listed.stdout


def test_template_list_hides_highlights_when_default_is_overridden(
    template_home: Path, monkeypatch: pytest.MonkeyPatch
):
    # $TREEBOX_TEMPLATE_DIR replaces every template with a box whose contents we
    # can't vouch for, so the bundled-image highlight must not be advertised.
    override = template_home / "custom"
    override.mkdir(parents=True)
    monkeypatch.setenv("TREEBOX_TEMPLATE_DIR", str(override))
    res = _cli(["template", "list"])
    assert res.exit_code == 0
    assert "Bundled 'default' template includes" not in res.stdout
    assert "overrides every template" in res.stderr
