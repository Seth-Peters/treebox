"""Ecosystem detection: which package managers a worktree uses, how to run
their cache-backed setup, and which files feed the lockfile hash.

Open question #3 (caches to manage per ecosystem) is answered here for uv, npm,
pnpm, go, and cargo. uv/npm/pnpm are the primary, fully-wired set; go/cargo are
detected and cache-redirected too.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Root under which shared host caches are bind-mounted inside the sandbox container.
CONTAINER_CACHE_ROOT = "/caches"


@dataclass(frozen=True)
class Ecosystem:
    """One package manager's full cache wiring — the single source of truth.

    Everything derived from an ecosystem (default host cache dir, setup env,
    container bind mounts) is computed from these fields, so adding an
    ecosystem is one entry here and nothing to mirror elsewhere.
    """

    name: str
    # Presence of any lockfile means this ecosystem is active. Lockfiles (plus
    # extra_manifests) are the files hashed to detect dependency changes.
    lockfiles: tuple[str, ...]
    extra_manifests: tuple[str, ...] = ()
    # Setup command, as argv, run in the worktree dir.
    command: tuple[str, ...] = ()
    # Cache key into Config.caches and the env var the tool reads it from.
    cache_key: str | None = None
    cache_env_var: str | None = None
    # Some tools take the store via a flag rather than env (e.g. pnpm).
    cache_flag: str | None = None
    # Ambient env var honored as the operator's override when computing the
    # default shared host cache dir; falls back to cache_env_var when None.
    host_cache_override: str | None = None
    # Conventional host cache location, given (home, xdg_cache).
    default_cache_dir: Callable[[Path, Path], str] | None = None
    # Env var pointing *in-container* tools at the mounted cache; falls back
    # to cache_env_var. pnpm takes its store via --store-dir on the host but
    # reads the npm-style env form, so containers get npm_config_store_dir.
    container_cache_env: str | None = None

    def manifest_files(self) -> tuple[str, ...]:
        return self.lockfiles + self.extra_manifests

    def container_cache_target(self) -> str | None:
        """Where this ecosystem's shared cache is bind-mounted in-container."""
        return f"{CONTAINER_CACHE_ROOT}/{self.name}" if self.cache_key else None

    def container_env_var(self) -> str | None:
        """Env var wiring in-container tools to ``container_cache_target()``."""
        return self.container_cache_env or self.cache_env_var

    def default_host_cache(self, home: Path, xdg_cache: Path) -> str | None:
        """Default shared host cache dir: the ambient env override when set,
        else the ecosystem's conventional location. None when uncached."""
        if not (self.cache_key and self.default_cache_dir):
            return None
        override_var = self.host_cache_override or self.cache_env_var
        override = os.environ.get(override_var) if override_var else None
        return override or self.default_cache_dir(home, xdg_cache)


ECOSYSTEMS: tuple[Ecosystem, ...] = (
    Ecosystem(
        name="uv",
        lockfiles=("uv.lock",),
        extra_manifests=("pyproject.toml",),
        command=("uv", "sync"),
        cache_key="uv",
        cache_env_var="UV_CACHE_DIR",
        default_cache_dir=lambda home, xdg_cache: str(xdg_cache / "uv"),
    ),
    Ecosystem(
        name="pnpm",
        lockfiles=("pnpm-lock.yaml",),
        extra_manifests=("package.json",),
        command=("pnpm", "install", "--frozen-lockfile"),
        cache_key="pnpm",
        cache_flag="--store-dir",
        host_cache_override="PNPM_HOME",
        default_cache_dir=lambda home, xdg_cache: str(home / ".local/share/pnpm/store"),
        container_cache_env="npm_config_store_dir",
    ),
    Ecosystem(
        name="npm",
        lockfiles=("package-lock.json",),
        extra_manifests=("package.json",),
        command=("npm", "ci"),
        cache_key="npm",
        cache_env_var="npm_config_cache",
        default_cache_dir=lambda home, xdg_cache: str(home / ".npm"),
    ),
    Ecosystem(
        name="go",
        lockfiles=("go.sum",),
        extra_manifests=("go.mod",),
        command=("go", "mod", "download"),
        cache_key="go",
        cache_env_var="GOMODCACHE",
        default_cache_dir=lambda home, xdg_cache: str(home / "go" / "pkg" / "mod"),
    ),
    Ecosystem(
        name="cargo",
        lockfiles=("Cargo.lock",),
        extra_manifests=("Cargo.toml",),
        command=("cargo", "fetch"),
        cache_key="cargo",
        cache_env_var="CARGO_HOME",
        default_cache_dir=lambda home, xdg_cache: str(home / ".cargo"),
    ),
)


def detect(worktree: str | Path) -> list[Ecosystem]:
    """Active ecosystems, in a stable order. pnpm wins over npm when both
    lockfiles exist (pnpm-lock.yaml present implies a pnpm project)."""
    root = Path(worktree)
    found = [eco for eco in ECOSYSTEMS if any((root / lf).is_file() for lf in eco.lockfiles)]
    names = {e.name for e in found}
    if "pnpm" in names and "npm" in names:
        found = [e for e in found if e.name != "npm"]
    return found


@dataclass
class SetupStep:
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


def _cache_dir_for(
    eco: Ecosystem, caches: dict[str, str], cold_cache_root: str | None
) -> str | None:
    """Resolve (and create) the cache dir feeding ``eco``'s setup.

    The single place the cold-vs-warm routing lives: with ``cold_cache_root``
    set the cache is a throwaway dir under it (never the shared store — the
    ``--cold`` isolation rule), else the operator-configured shared store.
    Returns None when the ecosystem has no cache or none is configured.
    """
    if not eco.cache_key:
        return None
    if cold_cache_root:
        cache_dir: str | None = str(Path(cold_cache_root) / eco.name)
    else:
        cache_dir = caches.get(eco.cache_key)
    if not cache_dir:
        return None
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    return cache_dir


def setup_steps(
    ecosystems: list[Ecosystem],
    caches: dict[str, str],
    *,
    cold_cache_root: str | None,
) -> list[SetupStep]:
    """Build cache-backed setup commands.

    When ``cold_cache_root`` is set, every ecosystem's cache is redirected into
    a throwaway directory under it, giving a clean from-source resolution that
    never touches the shared store (target-state ``--cold``).
    """
    steps: list[SetupStep] = []
    for eco in ecosystems:
        argv = list(eco.command)
        env: dict[str, str] = {}
        cache_dir = _cache_dir_for(eco, caches, cold_cache_root)
        if cache_dir:
            if eco.cache_env_var:
                env[eco.cache_env_var] = cache_dir
            if eco.cache_flag:
                argv += [eco.cache_flag, cache_dir]
        steps.append(SetupStep(eco.name, argv, env))
    return steps


def cache_env(caches: dict[str, str], *, cold_cache_root: str | None = None) -> dict[str, str]:
    """Env vars wiring every env-driven ecosystem cache to the shared store
    (or a throwaway dir under ``cold_cache_root``). Used by custom setup hooks."""
    env: dict[str, str] = {}
    for eco in ECOSYSTEMS:
        if not eco.cache_env_var:
            continue
        cache_dir = _cache_dir_for(eco, caches, cold_cache_root)
        if cache_dir:
            env[eco.cache_env_var] = cache_dir
    return env


def lockfile_hash(worktree: str | Path, ecosystems: list[Ecosystem] | None = None) -> str:
    """SHA-256 over the sorted contents of every present manifest file.

    Returns the empty string when no manifests exist (nothing to track).
    """
    root = Path(worktree)
    ecosystems = ecosystems if ecosystems is not None else detect(worktree)
    files = sorted({m for eco in ecosystems for m in eco.manifest_files()})
    h = hashlib.sha256()
    seen = False
    for name in files:
        p = root / name
        if not p.is_file():
            continue
        seen = True
        h.update(name.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest() if seen else ""
