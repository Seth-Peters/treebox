"""Resolve the sandbox container template directory.

The sandbox template is operator-owned and never read from the target repo: a
repo you don't trust must not define the container it runs in (mounts, runArgs,
and env can all reach the host). The agent runs *inside* the box; the config
that defines the box lives where a boxed agent cannot see or edit it.

Templates are selectable by name so devs can keep several sandbox shapes
(``python``, ``node``, a locked-down vs. a permissive one) side by side and pick
one per run with ``--template``. Resolution order for ``name``:

1. ``$TREEBOX_TEMPLATE_DIR`` (explicit dir; wins for any name)
2. ``$TREEBOX_HOME/templates/<name>`` (default ``~/.treebox/templates/<name>``)
3. the bundled package data (``assets/container``) — only for the default.

A named template that resolves to none of these is an error, not a silent
fallback to the default: asking for ``--template hardened`` and quietly getting
the stock box would defeat the point.
"""

from __future__ import annotations

import atexit
import os
from contextlib import ExitStack
from functools import cache
from importlib import resources
from pathlib import Path

from .config import treebox_home

DEFAULT_TEMPLATE = "default"

# The main "blocks" the bundled default image ships, curated from its
# Dockerfile — surfaced under `template list` so an operator can see what the
# stock sandbox gives them without reading the build recipe. This describes the
# *bundled* default only: a user template is whatever its owner made it, so the
# highlight is shown only when `default` still resolves to the bundled image.
DEFAULT_TEMPLATE_HIGHLIGHTS = (
    "Python 3.14 + uv",
    "Node.js 22 + npm",
    "Claude Code & Codex agent CLIs",
    "Playwright CLI + Chromium browser",
    "GitHub CLI (gh)",
    "AWS CLI",
    "git with delta diffs",
    "ripgrep, fd, fzf, jq, yq",
    "Egress firewall (iptables/ipset)",
)

# The container definition the docker runner renders and runs. Its schema is
# treebox-owned: build.{dockerfile,args}, user, mounts, env, runArgs, postCreate.
CONFIG_FILE = "container.json"

TEMPLATE_FILES = (
    CONFIG_FILE,
    "Dockerfile",
    "post-create.sh",
    "init-firewall.sh",
    "allowed-domains.sh",
)
FIREWALL_FILE = "firewall.json"

# ``resources.as_file`` may materialize the bundled template to a temporary
# location (zipped installs, unusual loaders) that is cleaned up when its
# context exits — so the context must outlive every caller of the returned
# path. One process-lifetime stack owns the materialization; the cache makes
# repeated resolutions reuse it instead of re-extracting.
_RESOURCE_LIFETIME = ExitStack()
atexit.register(_RESOURCE_LIFETIME.close)


@cache
def _bundled_template_dir() -> Path:
    p = _RESOURCE_LIFETIME.enter_context(
        resources.as_file(resources.files("treebox") / "assets" / "container")
    )
    return Path(p)


def user_templates_root() -> Path:
    """Where named, user-authored templates live: ``$TREEBOX_HOME/templates``
    (default ``~/.treebox/templates``). ``treebox template init`` scaffolds
    into here; ``template_dir`` resolves ``<name>`` against it."""
    return treebox_home() / "templates"


def template_dir(name: str = DEFAULT_TEMPLATE) -> Path:
    """Resolve the operator-owned template directory for ``name``.

    Never reads from the target repo — see the module docstring for why.
    """
    explicit = os.environ.get("TREEBOX_TEMPLATE_DIR")
    if explicit:
        return Path(explicit).expanduser()

    user = user_templates_root() / name
    if user.is_dir():
        return user

    if name == DEFAULT_TEMPLATE:
        return _bundled_template_dir()

    raise RuntimeError(
        f"No template named '{name}'. Create one at {user} "
        f"(or point $TREEBOX_TEMPLATE_DIR at a template dir). "
        f"'{DEFAULT_TEMPLATE}' is the only built-in template."
    )


def missing_template_files(path: Path) -> list[str]:
    """Which required ``TEMPLATE_FILES`` are absent from a template dir, in
    manifest order (empty list = a dir the docker runner can render). The
    docker runner throws on the first missing file at provision time; this is
    the same check surfaced early, for ``template list`` and ``init``."""
    return [f for f in TEMPLATE_FILES if not (path / f).is_file()]


def available_templates() -> list[str]:
    """Sorted names of selectable templates: the built-in ``default`` plus
    every directory under ``user_templates_root()``. ``$TREEBOX_TEMPLATE_DIR``
    is a single explicit dir that overrides *any* name rather than a named
    collection, so it does not enumerate here — callers surface it separately."""
    names = {DEFAULT_TEMPLATE}
    root = user_templates_root()
    if root.is_dir():
        names.update(p.name for p in root.iterdir() if p.is_dir())
    return sorted(names)
