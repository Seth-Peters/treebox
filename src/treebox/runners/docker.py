"""Docker runner: provision identically, but run the agent inside a plain
Docker container with shared caches + scoped copies of the login credentials
mounted in.

Buys isolation + a pinned toolchain + an optional firewall, so it is opt-in.
Docker is the only dependency: the container is built, started, and entered
with plain ``docker build`` / ``docker run`` / ``docker exec``. The image's
baked-in ``post-create.sh`` is the setup hook for this runner; shared caches
are wired in as bind mounts so containerized setup resolves from the same
store as the host.

Lockdown invariant: the files that *define* the sandbox (container.json and
its build context) are materialized in a host-side directory **outside** the
mounted worktree. The agent only ever has the worktree, its git common dir,
shared caches, and per-worktree credential copies mounted, so it cannot read
or edit the config that defines its own box. The config is regenerated from
the operator template on every run, so anything the agent writes inside the
mount is inert. See ``assets.py`` for where templates come from and why they
are never read from the repo.

Shared-``.git`` invariant: in-container git cannot commit/fetch without the
repo's git common dir, so it is mounted writable at its host path — meaning a
boxed agent can write objects/refs there. Those writes are inert data. The
danger is the paths git *executes* on the host (the operator's next
``treebox create``/``enter`` shells out to host git in the same repo): the
``hooks/`` dir and the exec-shaped config keys (``core.hooksPath``,
``core.fsmonitor``). Both are closed here: ``hooks/`` is re-mounted read-only
so the agent cannot plant a host-run hook, and every host-side git call pins
the exec-shaped config to inert values (see ``git.py`` ``_SAFE_CONFIG``), so a
redirect written into the shared config is ignored when treebox invokes git.
The agent's own in-container git is unaffected: commit/fetch/push/branch/
checkout never write hooks, and its per-invocation config is not touched.
Residual, by design: a *manual* host ``git`` the operator runs in the repo
outside treebox does not carry those flags, and a hostile ``credential.helper``
in the shared config is not neutralized (it collides with the fetch credential
cascade); treat the repo's ``.git`` as attacker-influenced after a sandboxed
session and prefer treebox's own commands over ad-hoc host git against it.
The pre-push guard's per-worktree hooks dir (``.git/worktrees/<id>/
treebox-hooks``, see provision.py) is the same residual class: agent-writable,
ignored by treebox's own pinned git, and consulted only by a manual host git
run inside that worktree.

Credential invariant: the operator's live ``~/.claude`` / ``~/.codex`` are
NEVER bind-mounted. They hold host-executed config (``settings.json`` hooks run
with the operator's full host privileges), so a writable mount would let a boxed
agent turn container access into host RCE. Instead ``_refresh_credentials``
asks each ``Harness`` registry entry to stage copies of just its login files
into a throwaway per-worktree dir, and *that* is mounted — writable, so
in-container token refresh and agent state still work, but every write lands in
the disposable copy.

Firewall invariant: when the operator enables the firewall, default-deny
egress must exist before any workspace-derived code runs. ``setup`` execs
``init-firewall.sh`` before ``post-create.sh``, and ``post-create.sh`` fails
closed on the readiness flag if that ordering is ever lost. Rules don't
survive a container restart, so every restart re-runs the (idempotent) init.
Capabilities can't be added to an existing container, so asking for the
firewall on a container created without it is a loud error, never a silent
open-egress run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict, cast

from .. import assets, git, system
from ..ecosystems import ECOSYSTEMS
from ..harnesses import HARNESSES, Harness
from ..models import Worktree, expand_user
from ..output import Reporter
from .base import PreflightError, RunnerFacts

if TYPE_CHECKING:
    from ..config import Config

# Sibling of the worktree root that holds each worktree's operator-owned
# container config + build context. The config/build files themselves are NEVER
# mounted into the container (only the worktree, its git common dir, and the
# scoped credential copies below), so a boxed agent can't reach them.
_CONFIG_ROOT = ".containers"

# Subdir of the per-worktree config dir holding the staged credential copies
# (one subdir per harness). Survives config regeneration so an existing
# container's bind mounts stay valid.
_CREDS_SUBDIR = "credentials"

# In-container user the agent runs as when the template doesn't set one.
_DEFAULT_USER = "agent"

# The container.json schema (also the firewall overlay's). ``_KNOWN_KEYS``
# is the runtime half of the contract: any key outside it is a loud error,
# not silently ignored — this is a security-sensitive config, and a typoed
# key that is silently dropped would strip behavior the operator relies on.
# ``ContainerConfig`` is the static half; ``_require_known_keys`` is where a
# freshly-parsed dict is validated and narrowed into it.


class BuildSection(TypedDict, total=False):
    dockerfile: str
    args: dict[str, str]


class ContainerConfig(TypedDict, total=False):
    build: BuildSection
    user: str
    mounts: list[str]
    env: dict[str, str]
    runArgs: list[str]
    postCreate: str


_KNOWN_KEYS = frozenset({"build", "user", "mounts", "env", "runArgs", "postCreate"})

# Keeps the detached sandbox container alive between execs. `docker run --init`
# puts a real PID 1 above it so orphaned grandchildren get reaped.
_KEEPALIVE = ("sleep", "infinity")

# Re-establishes egress lockdown, guarded on the env baked in at `docker run`:
# a no-op for containers created without the firewall, idempotent (the script
# skips itself when the rules are already live) for containers created with it.
_FIREWALL_GUARDED = (
    "sh",
    "-c",
    'if [ "$TREEBOX_FIREWALL" = "1" ]; then /usr/local/bin/init-firewall.sh; fi',
)


def _install_hint() -> str:
    """Platform-aware install instructions for a missing docker binary."""
    if sys.platform == "darwin":
        return (
            "Install Docker Desktop or OrbStack (or `brew install colima docker` + `colima start`)."
        )
    return "Install Docker Engine (https://docs.docker.com/engine/install/)."


def _daemon_hint() -> str:
    if sys.platform == "darwin":
        return (
            "Start Docker Desktop or OrbStack (or `colima start`), then verify with `docker info`."
        )
    return (
        "Start it (`sudo systemctl start docker`), then verify with `docker info` "
        "— if that fails with a permission error, add yourself to the docker "
        "group (`sudo usermod -aG docker $USER` + re-login)."
    )


def _sanitize(name: str) -> str:
    """A docker-safe name fragment: lowercase, ``[a-z0-9_.-]``, no leading or
    trailing separators (image tags must be lowercase; branch names aren't)."""
    safe = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-._")
    return safe or "worktree"


def _mount(
    source: Path | str, target: Path | str, *, kind: str = "bind", readonly: bool = False
) -> str:
    """A ``docker run --mount`` spec. Commas are CSV field separators in that
    syntax with no escaping, so a path containing one is a loud error here
    instead of a baffling daemon error after the image build already ran."""
    for p in (source, target):
        if "," in str(p):
            raise RuntimeError(
                f"Cannot mount a path containing ',': {p} "
                "(docker --mount syntax cannot express it). "
                "Rename the branch/path and re-create."
            )
    spec = f"type={kind},source={source},target={target}"
    if readonly:
        spec += ",readonly"
    return spec


# Credentials are copied from the host login dirs at setup time, so a missing
# login is advisory rather than a hard gate in `doctor`'s machine verdict.
_FACTS = RunnerFacts(preflight_detail="docker daemon ok", login_required=False)


class DockerRunner:
    name = "docker"

    def __init__(
        self,
        config: Config,
        *,
        remove_volumes: bool = False,
        docker: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        """``remove_volumes`` is this runner's teardown option (volumes are a
        docker concept — see ``teardown``). ``docker`` injects a canned engine
        CLI for tests; it defaults to the real subprocess seam and is invisible
        to ``get_runner`` callers — an internal constructor detail, not API."""
        self.config = config
        self._remove_volumes = remove_volumes
        self._docker_cli = docker

    # --- the engine seam -------------------------------------------------------

    def _engine(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Every docker invocation this runner makes goes through here: the
        injected engine when one was provided, else the module-level seam.
        Late-bound so patching the module seam still reaches CLI-built
        runners. Never raises on a non-zero exit."""
        return (self._docker_cli or _docker)(args)

    def _engine_check(self, args: list[str]) -> None:
        """Run docker through the seam, raising with the daemon's message on
        failure."""
        proc = self._engine(args)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(detail or f"docker {' '.join(args)} failed")

    def _run_engine(self, argv: list[str]) -> None:
        """Execute a full builder argv (leading "docker" included) through the
        seam."""
        if not argv or argv[0] != "docker":
            raise ValueError(f"expected a full docker argv, got {argv!r}")
        self._engine_check(argv[1:])

    def _available(self) -> bool:
        if self._docker_cli is not None:
            return self._docker_cli(["info"]).returncode == 0
        return _docker_available()

    # --- container queries (all through the engine seam) ----------------------

    def _container_ids(self, wt: Worktree) -> list[str]:
        out = self._engine(["ps", "-aq", "--filter", f"label={self._id_label(wt)}"]).stdout
        return [line for line in out.split() if line]

    def _container_state(self, container_id: str) -> ContainerState:
        out = self._engine(
            [
                "inspect",
                container_id,
                "--format",
                "{{.Name}}\n{{.State.Running}}\n{{range .Config.Env}}{{println .}}{{end}}",
            ]
        ).stdout.splitlines()
        name = out[0].strip().lstrip("/") if out else ""
        running = len(out) > 1 and out[1].strip() == "true"
        firewall = "TREEBOX_FIREWALL=1" in {line.strip() for line in out[2:]}
        return ContainerState(name, running, firewall)

    def _container_image(self, ids: list[str]) -> str | None:
        out = self._engine(["inspect", *ids, "--format", "{{.Config.Image}}"]).stdout.splitlines()
        return out[0].strip() if out else None

    def _container_volumes(self, ids: list[str]) -> list[str]:
        out = self._engine(
            [
                "inspect",
                *ids,
                "--format",
                '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\\n"}}{{end}}{{end}}',
            ]
        ).stdout
        return [v for v in out.splitlines() if v.startswith("treebox-")]

    # --- preflight -----------------------------------------------------------

    def preflight(self, reporter: Reporter) -> None:
        if not system.have("docker"):
            raise PreflightError(
                "docker runner needs docker on PATH.",
                hint=_install_hint(),
            )
        if not self._available():
            raise PreflightError(
                "docker is installed but the Docker daemon is not reachable.",
                error_code="DOCKER_UNAVAILABLE",
                hint=_daemon_hint(),
            )
        reporter.info("runner: docker (daemon reachable)")

    def facts(self) -> RunnerFacts:
        return _FACTS

    # --- locations & identity --------------------------------------------------

    def _config_dir(self, wt: Worktree) -> Path:
        """Host-side dir holding this worktree's container config + build
        context. A sibling of the worktree root, so it is never mounted in."""
        return wt.path.parent / _CONFIG_ROOT / wt.name

    def _config_file(self, wt: Worktree) -> Path:
        return self._config_dir(wt) / assets.CONFIG_FILE

    def _creds_dir(self, wt: Worktree) -> Path:
        """Per-worktree dir of scoped login-file copies (one subdir per harness).
        This — never the operator's live ``~/.claude`` / ``~/.codex`` — is what
        gets bind-mounted into the container."""
        return self._config_dir(wt) / _CREDS_SUBDIR

    def _id_label(self, wt: Worktree) -> str:
        """Stable container identity for queries (ps/teardown), keyed on the
        worktree path so every command agrees on the same container."""
        return f"treebox.workspace={wt.path}"

    def _slug(self, wt: Worktree) -> str:
        """Deterministic container name and image tag. The path hash makes it
        unique across repos whose branches share a name, and reproducible so
        ``run``, ``exec``, and ``--print`` all address the same container."""
        digest = hashlib.sha256(str(wt.path).encode()).hexdigest()[:10]
        return f"treebox-{_sanitize(wt.name)[:40]}-{digest}"

    # --- config rendering ------------------------------------------------------

    def _template_config(self) -> ContainerConfig:
        tpl = assets.template_dir(self.config.template)
        config = json.loads((tpl / assets.CONFIG_FILE).read_text(encoding="utf-8"))
        return _require_known_keys(config, assets.CONFIG_FILE)

    def _user(self, wt: Worktree) -> str:
        """The in-container user agent execs run as: from the rendered
        per-worktree config (which matches the container that was actually
        created), falling back to the template when none was rendered yet."""
        config: Mapping[str, Any]
        try:
            config = json.loads(self._config_file(wt).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = self._template_config()
        return str(config.get("user", _DEFAULT_USER))

    def _overlaid_config(self) -> ContainerConfig:
        """The operator template with the firewall overlay merged in, but before
        any per-worktree substitution — so ``${workspaceName}`` is still visible
        and a mount can be told apart as per-workspace vs. shared."""
        config = self._template_config()
        if self.config.firewall:
            tpl = assets.template_dir(self.config.template)
            fw = json.loads((tpl / assets.FIREWALL_FILE).read_text(encoding="utf-8"))
            _require_known_keys(fw, assets.FIREWALL_FILE)
            # The merge is shape-agnostic (recursive JSON); both sides were
            # validated above, so the result is still a ContainerConfig.
            _deep_merge(cast("dict[str, Any]", config), fw)
        return config

    def _merged_config(self, wt: Worktree, *, cold: bool) -> ContainerConfig:
        """The operator template with the firewall overlay and per-worktree
        mounts/env injected — the full description of the container to run.
        Pure computation: no filesystem writes (``--dry-run`` relies on it)."""
        config = self._overlaid_config()
        self._inject(config, wt, cold=cold)
        return config

    def _inject(self, config: ContainerConfig, wt: Worktree, *, cold: bool) -> None:
        ident = system.identity()
        build = config.setdefault("build", {})
        args = build.setdefault("args", {})
        # Pin ownership to the invoking user so worktree/bind files aren't root.
        args["USER_UID"] = str(ident.uid)
        args["USER_GID"] = str(ident.gid)
        # Host timezone wins over the template default when set.
        if os.environ.get("TZ"):
            args["TZ"] = os.environ["TZ"]

        mounts = config.setdefault("mounts", [])
        mounts[:] = [m.replace("${workspaceName}", _sanitize(wt.name)) for m in mounts]

        user = config.get("user", _DEFAULT_USER)
        # Scoped credential copies, not the live host login dirs (see
        # _refresh_credentials). Mounted in cold mode too: auth is not a cache.
        for harness in HARNESSES:
            staged, mount_target = harness.sandbox_mount(self._creds_dir(wt) / harness.name, user)
            mounts.append(_mount(staged, mount_target))

        # The worktree and its git common dir are mounted at their host paths,
        # mirrored 1:1, so every gitdir pointer inside the worktree resolves
        # identically in-container — plain `git status` just works, with no
        # relative-worktree or git-version requirement.
        mounts.append(_mount(wt.path, wt.path))
        common = git.common_dir(wt.repo)
        mounts.append(_mount(common, common))
        # The shared .git/hooks are executed by *host* git under its default
        # core.hooksPath (the operator's next `treebox create`/`enter`, or a
        # manual git in the repo). The common dir above is writable so
        # in-container git can commit/fetch, which would otherwise let a boxed
        # agent drop a host-run hook there. Mount hooks/ read-only so it can't:
        # commit/fetch/push/branch/checkout never write hooks, so the agent's
        # own git is unaffected. (Config-based redirects — core.hooksPath /
        # core.fsmonitor set in the shared config — are neutralized separately
        # by the safety flags on treebox's host-side git; see git.py.)
        mounts.append(_mount(common / "hooks", common / "hooks", readonly=True))

        if cold:
            return  # cold => no shared cache mounts; build from source in-image

        # Bind-mount every configured shared host cache and point the
        # in-container tools at it. Driven by ECOSYSTEMS (the single source of
        # cache wiring) so a new ecosystem can't silently miss container warmth.
        env = config.setdefault("env", {})
        for eco in ECOSYSTEMS:
            target = eco.container_cache_target()
            if not (eco.cache_key and target):
                continue
            host_dir = self.config.caches.get(eco.cache_key)
            if not host_dir:
                continue
            mounts.append(_mount(expand_user(host_dir), target))
            var = eco.container_env_var()
            if var:
                env[var] = target

    def _ensure_hook_dir(self, wt: Worktree) -> None:
        """Guarantee the read-only ``hooks/`` mount source exists before
        ``docker run`` (a ``--mount`` with a missing source is a hard error, and
        an absent dir would be a writable gap the mount is meant to cover). It is
        the repo's own ``.git/hooks``; git itself creates it, so materializing an
        empty one when absent is benign. Kept out of ``_inject`` so config
        rendering — which ``--dry-run`` uses — never touches the filesystem."""
        (git.common_dir(wt.repo) / "hooks").mkdir(parents=True, exist_ok=True)

    def _ensure_cache_dirs(self) -> None:
        """Create the configured shared cache dirs before ``docker run`` (a
        ``--mount`` with a missing source is an error, and the first worktree
        on a fresh host has no caches yet). Kept out of ``_inject`` so config
        rendering — which ``--dry-run`` uses — never touches the filesystem."""
        for eco in ECOSYSTEMS:
            if not (eco.cache_key and eco.container_cache_target()):
                continue
            host_dir = self.config.caches.get(eco.cache_key)
            if host_dir:
                expand_user(host_dir).mkdir(parents=True, exist_ok=True)

    def _write_config(self, wt: Worktree, *, cold: bool) -> ContainerConfig:
        """Render the operator template into the host-side config dir (outside
        the worktree) and return the merged config. Regenerated from scratch
        each run."""
        tpl = assets.template_dir(self.config.template)
        dc = self._config_dir(wt)
        # Regenerate cleanly: the operator template is the single source of
        # truth; never trust leftovers from a previous run. The credentials
        # subdir is spared (an existing container's bind mounts point at it);
        # its *contents* are refreshed from the host by _refresh_credentials.
        if dc.exists():
            for child in dc.iterdir():
                if child.name == _CREDS_SUBDIR:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        dc.mkdir(parents=True, exist_ok=True)
        for f in assets.TEMPLATE_FILES:
            src = tpl / f
            if not src.is_file():
                raise RuntimeError(f"{f} not found in template dir: {tpl}")
            shutil.copy(src, dc / f)
        for script in ("post-create.sh", "init-firewall.sh"):
            p = dc / script
            if p.exists():
                p.chmod(0o755)
        # The config dir doubles as the build context; keep the staged
        # credential copies out of what gets sent to the docker daemon.
        (dc / ".dockerignore").write_text(f"{_CREDS_SUBDIR}/\n", encoding="utf-8")

        config = self._merged_config(wt, cold=cold)
        self._config_file(wt).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return config

    def _refresh_credentials(self, wt: Worktree) -> None:
        """Stage every harness's scoped login-file copies into the per-worktree
        credential dirs that get bind-mounted.

        Never mounts — or writes back to — the operator's live ``~/.claude`` /
        ``~/.codex``: those hold host-executed config (settings.json hooks), so
        a boxed agent must not be able to write them. Copies are refreshed on
        every provision — ``setup`` on create, ``refresh`` on every enter —
        with ``Harness.stage_credentials`` copy-or-drop semantics, so both a
        re-login and a revocation propagate on the next entry.
        """
        for harness in HARNESSES:
            harness.stage_credentials(self._creds_dir(wt) / harness.name)

    def refresh(self, wt: Worktree, *, reporter: Reporter) -> None:
        """Always-run entry refresh: re-stage the credential copies from the
        host. ``enter`` skips ``setup`` entirely when the lockfile hash is
        unchanged (the common case), and auth must not ride that cache — a
        host logout/revocation or a fresh login has to reach the sandbox on
        the very next entry, not whenever dependencies happen to change."""
        self._refresh_credentials(wt)
        reporter.ok("credentials", "scoped copies refreshed")

    # --- docker argv builders --------------------------------------------------
    # Builders return the full argv (leading "docker" included) so they can be
    # shown verbatim by --dry-run and reporter steps; _run_engine executes one.

    def _build_command(self, wt: Worktree, config: ContainerConfig) -> list[str]:
        dc = self._config_dir(wt)
        build = config.get("build", {})
        argv = [
            "docker",
            "build",
            "-t",
            self._slug(wt),
            "-f",
            str(dc / build.get("dockerfile", "Dockerfile")),
        ]
        for key, value in build.get("args", {}).items():
            argv += ["--build-arg", f"{key}={value}"]
        argv.append(str(dc))
        return argv

    def _run_command(self, wt: Worktree, config: ContainerConfig) -> list[str]:
        argv = [
            "docker",
            "run",
            "-d",
            "--init",
            "--name",
            self._slug(wt),
            "--label",
            self._id_label(wt),
        ]
        argv += [str(a) for a in config.get("runArgs", [])]
        for key, value in config.get("env", {}).items():
            argv += ["-e", f"{key}={value}"]
        for mount in config.get("mounts", []):
            argv += ["--mount", mount]
        argv += [self._slug(wt), *_KEEPALIVE]
        return argv

    def _exec_command(
        self,
        wt: Worktree,
        argv: list[str],
        *,
        user: str | None = None,
        stdin: bool = False,
        tty: bool = False,
    ) -> list[str]:
        cmd = ["docker", "exec"]
        if stdin:
            cmd.append("-i")
        if tty:
            cmd.append("-t")
        cmd += ["-u", user or self._user(wt), "-w", str(wt.path), self._slug(wt), *argv]
        return cmd

    # --- setup -------------------------------------------------------------------

    def setup(self, wt: Worktree, *, cold: bool, reporter: Reporter) -> None:
        with reporter.task("sandbox files", "templates written (outside worktree)"):
            config = self._write_config(wt, cold=cold)
        with reporter.task("credentials", "scoped copies staged (outside worktree)"):
            self._refresh_credentials(wt)

        existing = self._container_ids(wt)
        if existing:
            name, running, firewall = self._container_state(existing[0])
            self._require_own_container(wt, name)
            # Capabilities can't be added to an existing container: a firewall
            # request the container can't honor must fail loudly, or workspace
            # setup would run with open egress while claiming lockdown.
            if self.config.firewall and not firewall:
                raise RuntimeError(
                    "The firewall was requested, but this worktree's container "
                    "was created without it. Re-create the sandbox: "
                    f"treebox teardown {wt.name} && "
                    f"treebox create {wt.name} --isolation {self.name} --firewall"
                )
            if cold:
                reporter.warn(
                    "existing container keeps its creation-time cache mounts; "
                    "teardown and re-create for a fully cold sandbox"
                )
            if running:
                reporter.note("container", f"reusing {existing[0]}")
            else:
                with reporter.task("container", "started (existing)"):
                    self._engine_check(["start", existing[0]])
        else:
            firewall = self.config.firewall
            if not cold:
                self._ensure_cache_dirs()
            self._ensure_hook_dir(wt)
            env = dict(os.environ)
            # Line-buffered build output, so a failure dump reads cleanly.
            env.setdefault("BUILDKIT_PROGRESS", "plain")
            reporter.step("image", "built", self._build_command(wt, config), env=env)
            with reporter.task("container", "started"):
                self._run_engine(self._run_command(wt, config))

        # Egress lockdown BEFORE post-create: setup runs code derived from the
        # untrusted workspace (uv sync executes the repo's build backend), and
        # post-create.sh fails closed if this ordering is ever lost.
        if firewall:
            reporter.step("firewall", "egress locked down", self._firewall_command(wt))
        post_create = config.get("postCreate")
        if post_create:
            reporter.step(
                "setup",
                "post-create complete",
                self._exec_command(wt, ["sh", "-c", str(post_create)]),
            )

    def _firewall_command(self, wt: Worktree) -> list[str]:
        return self._exec_command(wt, list(_FIREWALL_GUARDED), user="root")

    def _require_own_container(self, wt: Worktree, name: str) -> None:
        """Refuse to adopt a container this runner didn't create: its name,
        mounts, and user don't match what exec expects, so entering it would
        misbehave."""
        if name != self._slug(wt):
            raise RuntimeError(
                f"This worktree's container '{name}' was not created by the docker "
                f"runner. Remove it and re-create: treebox teardown {wt.name}"
            )

    # --- launch / teardown ---------------------------------------------------

    def dry_run_setup(self, wt: Worktree) -> list[str]:
        config = self._merged_config(wt, cold=False)
        cmds = [
            f"# render {self.config.template} template into {self._config_dir(wt)} "
            "(operator-owned, outside the worktree)",
            f"# stage scoped login-file copies into {self._creds_dir(wt)} "
            "(the live ~/.claude / ~/.codex are never mounted)",
            shlex.join(self._build_command(wt, config)),
            shlex.join(self._run_command(wt, config)),
        ]
        if self.config.firewall:
            cmds.append(shlex.join(self._firewall_command(wt)))
        post_create = config.get("postCreate")
        if post_create:
            cmds.append(shlex.join(self._exec_command(wt, ["sh", "-c", str(post_create)])))
        return cmds

    def entry_command(self, wt: Worktree, *, harness: Harness, args: list[str]) -> list[str]:
        # "-i" always, so stdin piped into the printed command reaches the
        # agent; "-t" only when a terminal is attached, so the command emitted
        # by --print/--json also works when a script replays it off-TTY.
        return self._exec_command(
            wt,
            harness.launch_argv(args),
            stdin=True,
            tty=sys.stdin.isatty(),
        )

    def launch(self, wt: Worktree, *, harness: Harness, args: list[str]) -> int:
        self._ensure_running(wt)
        return subprocess.run(self.entry_command(wt, harness=harness, args=args)).returncode

    def _ensure_running(self, wt: Worktree) -> None:
        """Start the container if it stopped since setup (host reboot, manual
        stop) and re-establish the firewall — iptables rules don't survive a
        restart, and the guarded init is a no-op when no firewall was baked in."""
        ids = self._container_ids(wt)
        if not ids:
            raise RuntimeError(
                f"No container exists for this worktree — re-create it: "
                f"treebox teardown {wt.name} && treebox create {wt.name} --isolation {self.name}"
            )
        name, running, _ = self._container_state(ids[0])
        self._require_own_container(wt, name)
        if not running:
            self._engine_check(["start", ids[0]])
            self._run_engine(self._firewall_command(wt))

    def _template_volumes(self, wt: Worktree) -> list[str]:
        """The treebox volume names the template's ``${workspaceName}``
        substitution produces for this worktree — the same deterministic
        naming used at creation, so the volumes stay discoverable even when
        the container that mounted them is already gone. Only *per-workspace*
        volumes (whose source template contains ``${workspaceName}``, so the
        rendered name is unique to this worktree) qualify: a shared volume with
        a static name is owned by no single worktree, so one worktree's teardown
        must never reclaim it."""
        try:
            config = self._overlaid_config()
        except Exception:
            return []  # template unreadable: nothing to derive, best-effort
        volumes = []
        for spec in config.get("mounts", []):
            fields = dict(f.split("=", 1) for f in str(spec).split(",") if "=" in f)
            source = fields.get("source", "")
            if fields.get("type") != "volume" or not source.startswith("treebox-"):
                continue
            if "${workspaceName}" not in source:
                continue  # shared/static volume: not this worktree's to remove
            volumes.append(source.replace("${workspaceName}", _sanitize(wt.name)))
        return volumes

    def teardown(self, wt: Worktree, *, reporter: Reporter) -> None:
        """Remove the worktree's container/image (and, when this runner was
        constructed with ``remove_volumes``, its per-workspace volumes)."""
        if not self._available():
            reporter.note("container", "skipped; Docker unavailable")
            return
        ids = self._container_ids(wt)
        # Read the container's mounts BEFORE rm; and never rely on them alone —
        # when the container is already gone (manual docker rm, or a prior
        # teardown without --remove-volumes) the template-derived names are the
        # only way to find the volumes, or they leak forever.
        container_volumes = self._container_volumes(ids) if ids else []
        volumes: list[str] = []
        if self._remove_volumes:
            # Container-derived names exist by definition; template-derived
            # ones are only candidates — filter them against the live volume
            # list so we never try to rm (or report) a volume that isn't there.
            existing = set(self._engine(["volume", "ls", "-q"]).stdout.split())
            volumes = sorted(set(container_volumes) | (set(self._template_volumes(wt)) & existing))
        if ids:
            image = self._container_image(ids)
            self._engine(["rm", "-f", *ids])
            reporter.ok("container", f"removed {' '.join(ids)}")
            # Only images we know we built: this runner's treebox-* tags.
            if image and image.startswith("treebox-"):
                self._engine(["image", "rm", image])
        else:
            reporter.note("container", "none found")
        if self._remove_volumes:
            if volumes:
                self._engine(["volume", "rm", *volumes])
                reporter.ok("volumes", f"removed {' '.join(volumes)}")
        elif container_volumes:
            reporter.note("volumes", f"kept {' '.join(container_volumes)}")

        # Remove the host-side config/build-context dir too.
        cfg_dir = self._config_dir(wt)
        if cfg_dir.exists():
            shutil.rmtree(cfg_dir, ignore_errors=True)
            reporter.ok("sandbox files", "removed")


# --- docker helpers ----------------------------------------------------------


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    """The real docker CLI seam (mirrors git.py's ``_run``) — the engine every
    runner uses unless a canned one was injected at its constructor. Never
    raises on a non-zero exit; callers inspect the returncode/stdout they
    care about."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
    )


def _docker_available() -> bool:
    if not system.have("docker"):
        return False
    return _docker(["info"]).returncode == 0


class ContainerState(NamedTuple):
    """One container's state, from a single inspect: the name (adoption
    check), whether it is running, and whether it was created with the
    firewall baked in (``TREEBOX_FIREWALL=1`` in its env)."""

    name: str
    running: bool
    firewall: bool


def _require_known_keys(config: dict[str, Any], source: str) -> ContainerConfig:
    """Reject unknown top-level keys instead of silently ignoring them. A
    passing config is the schema ``_KNOWN_KEYS`` enforces, so this is also
    where a freshly-parsed dict is narrowed to ``ContainerConfig``."""
    unknown = sorted(set(config) - _KNOWN_KEYS)
    if unknown:
        raise RuntimeError(
            f"Unsupported key(s) in {source}: {', '.join(unknown)}. "
            f"Supported keys: {', '.join(sorted(_KNOWN_KEYS))}."
        )
    return cast(ContainerConfig, config)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        elif isinstance(value, list) and isinstance(base.get(key), list):
            base[key] = base[key] + value
        else:
            base[key] = value
