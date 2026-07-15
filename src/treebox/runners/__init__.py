"""Pluggable runners: the run seam that decides where the agent executes.

``RUNNERS`` is the isolation registry — the single place a runner is wired
into the CLI vocabulary. Each entry maps a runner's name to a small factory
that knows which generic options its adapter consumes (the host runner has no
per-worktree volumes, so the teardown flow's ``remove_volumes`` never reaches
it). ``VALID_ISOLATION`` — and with it config validation and the
``--isolation`` help text — derives from the registry. The ``config.Isolation``
Literal alias is intentionally hard-coded for internal typing, so adding a
runner also updates that alias; the drift test pins the registry and alias
together.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from .base import PreflightError, Runner, RunnerFacts, RunnerTeardownResult
from .docker import DockerRunner
from .host import HostRunner

if TYPE_CHECKING:
    from ..config import Config

RUNNERS: Final[dict[str, Callable[[Config, bool, list[str] | None], Runner]]] = {
    HostRunner.name: lambda config, _remove_volumes, _recorded_volumes: HostRunner(config),
    DockerRunner.name: lambda config, remove_volumes, recorded_volumes: DockerRunner(
        config, remove_volumes=remove_volumes, recorded_volumes=recorded_volumes
    ),
}

VALID_ISOLATION: tuple[str, ...] = tuple(RUNNERS)


def get_runner(
    config: Config,
    *,
    remove_volumes: bool = False,
    recorded_volumes: list[str] | None = None,
) -> Runner:
    """Resolve the configured isolation mode through the registry.

    ``remove_volumes`` is the teardown flow's ``--remove-volumes`` choice and
    ``recorded_volumes`` the per-workspace volume names recorded in the
    worktree state at create time; each factory forwards them only when its
    runner owns per-worktree volumes. An unknown mode is a loud error -
    defense-in-depth behind ``validate_config``, never a silent default.
    """
    try:
        factory = RUNNERS[config.isolation]
    except KeyError:
        raise ValueError(f"Unknown isolation mode '{config.isolation}'.") from None
    return factory(config, remove_volumes, recorded_volumes)


__all__ = [
    "RUNNERS",
    "VALID_ISOLATION",
    "DockerRunner",
    "HostRunner",
    "PreflightError",
    "Runner",
    "RunnerFacts",
    "RunnerTeardownResult",
    "get_runner",
]
