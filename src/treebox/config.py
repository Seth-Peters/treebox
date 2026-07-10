"""Typed configuration with user-level TOML discovery.

Config is user-level only by design (open question #2): a repo-level config could
run arbitrary commands on the host, so we never read settings from the target repo.
Resolution order, highest priority first:

1. Explicit CLI flags (applied by the caller, not here).
2. ``$TREEBOX_CONFIG`` if set, else ``$TREEBOX_HOME/config.toml`` (default
   ``~/.treebox/config.toml``).
3. Built-in defaults below.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from .ecosystems import ECOSYSTEMS
from .harnesses import VALID_HARNESSES
from .models import expand_user

# The closed vocabularies treebox understands, validated against the
# registry-derived VALID_* tuples (harnesses.HARNESSES, runners.RUNNERS —
# the latter imported inside validate_config: the runners package reaches
# back to this module at runtime via assets, so a module-level import here
# would be a cycle). Boundary values (TOML, CLI flags, state files) remain
# plain ``str``; deeper seams resolve them once to Harness/Runner objects.
# The Literal aliases are hard-coded for static typing, and a drift test
# asserts each alias still matches its registry.
Isolation = Literal["host", "docker"]
Harness = Literal["claude", "codex"]

DEFAULT_ROOT_REL = ".treebox/worktrees"
DEFAULT_BASE = "main"
DEFAULT_ISOLATION = "host"
DEFAULT_HARNESS = "claude"
DEFAULT_ENV_FILE = ".env"
DEFAULT_TEMPLATE = "default"


@dataclass(frozen=True)
class Config:
    """Resolved settings for a single invocation."""

    isolation: str = DEFAULT_ISOLATION
    harness: str = DEFAULT_HARNESS
    base: str = DEFAULT_BASE
    root: str = DEFAULT_ROOT_REL
    env_file: str = DEFAULT_ENV_FILE
    firewall: bool = False
    # Operator-owned container template to render the sandbox from. Resolved
    # by assets.template_dir() — never read from the target repo.
    template: str = DEFAULT_TEMPLATE
    # Setup hook override. When None, setup is auto-detected from manifests.
    # A list of shell command strings, each run in the worktree dir.
    setup_hook: list[str] | None = None
    # Host cache directories to share across worktrees and (for the docker
    # runner) bind-mount into the container. Keyed by ecosystem.
    caches: dict[str, str] = field(default_factory=dict)

    def with_overrides(
        self,
        *,
        isolation: str | None = None,
        harness: str | None = None,
        base: str | None = None,
        root: str | None = None,
        env_file: str | None = None,
        firewall: bool | None = None,
        template: str | None = None,
        setup_hook: list[str] | None = None,
        caches: dict[str, str] | None = None,
    ) -> Config:
        """Return a copy with non-None overrides applied. Values are applied
        as given — callers run ``validate_config`` after flag overrides."""
        overrides: dict[str, Any] = {
            "isolation": isolation,
            "harness": harness,
            "base": base,
            "root": root,
            "env_file": env_file,
            "firewall": firewall,
            "template": template,
            "setup_hook": setup_hook,
            "caches": caches,
        }
        applied = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **applied)


def treebox_home() -> Path:
    """The home-level treebox directory: ``$TREEBOX_HOME`` or ``~/.treebox``.

    One folder owns everything treebox keeps for you globally — ``config.toml``
    and ``templates/``. Its name is deliberately the same ``.treebox`` used for
    the per-repo worktrees dir, mirroring Claude Code's ``~/.claude`` vs. a
    project's ``.claude``: the scope is read from *where* the folder is (home vs.
    repo), not from a different name.
    """
    override = os.environ.get("TREEBOX_HOME")
    if override:
        return expand_user(override)
    return Path.home() / ".treebox"


def config_path() -> Path:
    explicit = os.environ.get("TREEBOX_CONFIG")
    if explicit:
        return expand_user(explicit)
    return treebox_home() / "config.toml"


def _expand_user_path(value: str | None) -> str | None:
    return str(expand_user(value)) if value is not None else None


def default_caches() -> dict[str, str]:
    """Default shared cache locations per ecosystem (open question #3).

    Derived from ``ECOSYSTEMS`` — the single source of cache wiring — honoring
    each tool's standard env override so the host and container agree.
    """
    home = Path.home()
    xdg_override = os.environ.get("XDG_CACHE_HOME")
    xdg_cache = expand_user(xdg_override) if xdg_override else home / ".cache"
    caches: dict[str, str] = {}
    for eco in ECOSYSTEMS:
        default = eco.default_host_cache(home, xdg_cache)
        if eco.cache_key and default:
            caches[eco.cache_key] = str(expand_user(default))
    return caches


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML, falling back to defaults.

    A missing file at the default discovery path (``$TREEBOX_HOME/config.toml``,
    default ``~/.treebox/config.toml``) is fine (defaults apply). A missing file
    at an explicitly-set ``$TREEBOX_CONFIG`` is a loud ValueError: setting the
    variable asserts the file exists, and silently no-op'ing a typo'd path would
    run with the wrong isolation/base/caches and zero indication anything was
    off. Raises ValueError on malformed content."""
    explicit = os.environ.get("TREEBOX_CONFIG") if path is None else None
    path = path or config_path()
    cfg = Config(caches=default_caches())
    if not path.is_file():
        if explicit:
            raise ValueError(f"$TREEBOX_CONFIG points at a missing config file: {path}")
        return cfg
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ValueError(f"Could not read config {path}: {exc}") from exc

    setup_hook: list[str] | None = None
    if "setup_hook" in data:
        hook = data["setup_hook"]
        if isinstance(hook, str):
            setup_hook = [hook]
        elif isinstance(hook, list) and all(isinstance(h, str) for h in hook):
            setup_hook = list(hook)
        else:
            raise ValueError(
                f"Invalid config {path}: setup_hook must be a string or a list of strings."
            )
    caches = dict(cfg.caches)
    if isinstance(data.get("caches"), dict):
        caches.update({str(k): str(expand_user(str(v))) for k, v in data["caches"].items()})

    cfg = cfg.with_overrides(
        isolation=_typed(data, "isolation", str, path),
        harness=_typed(data, "harness", str, path),
        base=_typed(data, "base", str, path),
        root=_expand_user_path(_typed(data, "root", str, path)),
        env_file=_expand_user_path(_typed(data, "env_file", str, path)),
        firewall=_typed(data, "firewall", bool, path),
        template=_typed(data, "template", str, path),
        setup_hook=setup_hook,
        caches=caches,
    )
    validate_config(cfg)
    return cfg


def _typed(data: dict[str, Any], key: str, want: type, path: Path) -> Any:
    """``data[key]`` (None when absent) after checking its TOML type, so a
    wrong-typed value is a clean ValueError — the INVALID_CONFIG contract —
    instead of a TypeError crash or a silently-misbehaving setting downstream.
    Exact-type check, not isinstance: ``bool`` is an ``int`` subclass, and
    ``firewall = 1`` should be rejected like any other type mismatch."""
    value = data.get(key)
    if value is not None and type(value) is not want:
        noun = {"str": "string", "bool": "boolean (true/false)"}.get(want.__name__, want.__name__)
        raise ValueError(
            f"Invalid config {path}: {key} must be a {noun}, got {type(value).__name__}."
        )
    return value


def validate_config(cfg: Config) -> None:
    """Raise ValueError when the resolved settings name modes treebox lacks."""
    from .runners import VALID_ISOLATION

    if cfg.isolation not in VALID_ISOLATION:
        raise ValueError(f"Invalid isolation mode '{cfg.isolation}'. Use one of {VALID_ISOLATION}.")
    if cfg.harness not in VALID_HARNESSES:
        raise ValueError(f"Invalid harness '{cfg.harness}'. Use one of {VALID_HARNESSES}.")
