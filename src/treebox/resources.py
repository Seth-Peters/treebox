"""The generic resource model: CPU, memory, and process limits for a sandbox.

One vocabulary for every runner. The operator sets limits per worktree with
``--cpus`` / ``--memory`` / ``--pids-limit``, as defaults in the user config's
``[resources]`` table (``cpus``, ``memory``, ``pids``), or in the operator
template's ``resources`` object. Precedence, field by field: explicit flag,
then user config, then the template. An omitted value means no treebox cap.
Nothing here is ever read from the target repo.

Each runner declares which limits it can apply (``Runner.supported_resources``)
and maps them to its own engine (the docker runner: ``docker run --cpus /
--memory / --pids-limit``). The effective limits are recorded in the
host-owned worktree record (``record.py``) when the worktree is created, and
are what the sandbox has from then on.

Value grammar (Docker's own, so a value that passes here is one Docker takes):

- ``cpus``: a decimal number of CPUs, at least 0.01, at most 9 decimal places
  (``2``, ``1.5``). The engine's CPU count is the upper bound (checked by the
  runner against the live engine).
- ``memory``: Docker's memory size syntax: a number and an optional unit
  ``b``, ``k``, ``m``, ``g``, ``t`` or ``p`` (binary multiples, an optional
  trailing ``b`` or ``ib``, any case): ``512m``, ``4g``, ``1.5g``, ``4GB``.
  A bare number is bytes. At least 6 MiB (Docker's minimum). Stored in a
  canonical form (``4GB`` and ``4096m`` both become ``4g``).
- ``pids``: a positive whole number of processes (``512``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

# The generic limit names, in display order. They are the ``[resources]``
# keys, the template ``resources`` keys, and the record keys.
RESOURCE_NAMES: tuple[str, ...] = ("cpus", "memory", "pids")

# The treebox CLI flag for each generic limit.
FLAGS: dict[str, str] = {"cpus": "--cpus", "memory": "--memory", "pids": "--pids-limit"}

MIN_CPUS = Decimal("0.01")
MIN_MEMORY_BYTES = 6 * 1024 * 1024

# Docker's memory size grammar (go-units RAMInBytes): number, optional space,
# optional unit, optional "i", optional "b".
_MEMORY_RE = re.compile(r"^(\d+(?:\.\d+)?) ?([kmgtp])?(i?b)?$", re.IGNORECASE)
_UNITS = "bkmgtp"


class ResourceLimitError(ValueError):
    """A limit value treebox (or the engine) cannot accept. ``name`` is the
    generic limit name (``cpus``, ``memory`` or ``pids``)."""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name


@dataclass(frozen=True)
class ResourceLimits:
    """Per-worktree limits; ``None`` is no treebox cap for that resource.

    Values are already validated and canonical: ``cpus`` a float, ``memory``
    a canonical Docker size string, ``pids`` a positive int."""

    cpus: float | None = None
    memory: str | None = None
    pids: int | None = None

    def names(self) -> list[str]:
        """The limits that are set, in display order."""
        return [n for n in RESOURCE_NAMES if getattr(self, n) is not None]

    def over(self, lower: ResourceLimits) -> ResourceLimits:
        """These limits, with each unset one taken from ``lower``."""
        return ResourceLimits(
            **{
                n: getattr(self, n) if getattr(self, n) is not None else getattr(lower, n)
                for n in RESOURCE_NAMES
            }
        )

    def only(self, names: Iterable[str]) -> ResourceLimits:
        """These limits with every resource outside ``names`` unset."""
        keep = set(names)
        return replace(self, **{n: None for n in RESOURCE_NAMES if n not in keep})

    def to_json(self) -> dict[str, Any]:
        """The record / ``--json`` shape: every key present, ``null`` = no cap."""
        return {
            "cpus": _cpus_number(self.cpus) if self.cpus is not None else None,
            "memory": self.memory,
            "pids": self.pids,
        }

    def cli_flags(self) -> list[str]:
        """The treebox flags that restate these limits (for recovery hints)."""
        argv: list[str] = []
        for n in self.names():
            value = getattr(self, n)
            argv += [FLAGS[n], format_value(n, value)]
        return argv

    def describe(self) -> str:
        """One human line: ``cpus 2 · memory 4g · pids 512``, or ``none``."""
        parts = [f"{n} {format_value(n, getattr(self, n))}" for n in self.names()]
        return " · ".join(parts) if parts else "none"


def format_value(name: str, value: Any) -> str:
    """A limit value as the operator would type it."""
    if name == "cpus":
        return str(_cpus_number(value))
    return str(value)


def _cpus_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def parse_cpus(raw: Any, *, source: str = "--cpus") -> float:
    """Validate a CPU count (a string from the CLI, a number from TOML/JSON)."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ResourceLimitError("cpus", f"Invalid {source} {raw!r}: use a number of CPUs, e.g. 2.")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise ResourceLimitError(
            "cpus", f"Invalid {source} {raw!r}: use a number of CPUs, e.g. 2 or 1.5."
        ) from None
    if not value.is_finite() or value < MIN_CPUS:
        raise ResourceLimitError(
            "cpus", f"Invalid {source} {raw!r}: the minimum is {MIN_CPUS} CPUs."
        )
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -9:
        raise ResourceLimitError("cpus", f"Invalid {source} {raw!r}: use at most 9 decimal places.")
    return float(value)


def parse_memory(raw: Any, *, source: str = "--memory") -> str:
    """Validate a Docker memory size and return its canonical form."""
    match = _MEMORY_RE.match(raw.strip()) if isinstance(raw, str) else None
    if match is None:
        raise ResourceLimitError(
            "memory",
            f"Invalid {source} {raw!r}: use Docker's memory syntax, a number and a "
            'unit b, k, m, g, t or p, e.g. "512m" or "4g".',
        )
    number, unit, _suffix = match.groups()
    power = _UNITS.index(unit.lower()) if unit else 0
    size = int(Decimal(number) * (1024**power))
    if size < MIN_MEMORY_BYTES:
        raise ResourceLimitError(
            "memory", f"Invalid {source} {raw!r}: the minimum is 6m (Docker's minimum)."
        )
    return _canonical_memory(size)


def _canonical_memory(size: int) -> str:
    """The largest unit that expresses ``size`` exactly: 4294967296 -> ``4g``."""
    for power in range(len(_UNITS) - 1, 0, -1):
        if size % (1024**power) == 0:
            return f"{size // 1024**power}{_UNITS[power]}"
    return str(size)


def parse_pids(raw: Any, *, source: str = "--pids-limit") -> int:
    """Validate a process-count limit (a string from the CLI, an int from TOML)."""
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and re.fullmatch(r"\s*[+-]?\d+\s*", raw):
        value = int(raw)
    else:
        value = None
    if value is None or value < 1:
        raise ResourceLimitError(
            "pids",
            f"Invalid {source} {raw!r}: use a positive whole number of processes, "
            "e.g. 512 (omit it for no limit).",
        )
    return value


_TABLE_TYPES: dict[str, tuple[type, ...]] = {"cpus": (int, float), "memory": (str,), "pids": (int,)}
_TABLE_NOUNS = {"cpus": "a number", "memory": 'a size string such as "4g"', "pids": "an integer"}
_PARSERS: dict[str, Any] = {"cpus": parse_cpus, "memory": parse_memory, "pids": parse_pids}


def from_flags(*, cpus: str | None, memory: str | None, pids: str | None) -> ResourceLimits:
    """Limits from the ``--cpus`` / ``--memory`` / ``--pids-limit`` flags."""
    return ResourceLimits(
        cpus=parse_cpus(cpus) if cpus is not None else None,
        memory=parse_memory(memory) if memory is not None else None,
        pids=parse_pids(pids) if pids is not None else None,
    )


def from_mapping(data: Any, *, source: str) -> ResourceLimits:
    """Limits from a ``{cpus, memory, pids}`` table: the user config's
    ``[resources]``, the template's ``resources`` object, or the record.
    Unknown keys are refused (a typo would be a cap silently not applied);
    a ``null`` value is no cap."""
    if not isinstance(data, Mapping):
        raise ResourceLimitError("resources", f"{source} must be a table of cpus, memory and pids.")
    unknown = sorted(set(data) - set(RESOURCE_NAMES))
    if unknown:
        raise ResourceLimitError(
            unknown[0],
            f"{source} has unknown key(s) {', '.join(unknown)} "
            f"(known: {', '.join(RESOURCE_NAMES)}).",
        )

    def value(name: str) -> Any:
        raw = data.get(name)
        if raw is None:
            return None
        # Typed like every other config value: a quoted "512" is a mistake.
        if isinstance(raw, bool) or not isinstance(raw, _TABLE_TYPES[name]):
            raise ResourceLimitError(
                name, f"{source} {name} must be {_TABLE_NOUNS[name]}, got {raw!r}."
            )
        return _PARSERS[name](raw, source=f"{source} {name}")

    return ResourceLimits(cpus=value("cpus"), memory=value("memory"), pids=value("pids"))
