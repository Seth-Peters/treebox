"""Which repo paths count as factory code (factory.yaml `factory_code`)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

from factory_cli.manifest import FactoryCode


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    """Glob to regex: `**` crosses directories, `*` and `?` don't."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, pattern: str) -> bool:
    return _compile(pattern).match(path) is not None


@dataclass(frozen=True)
class Violation:
    path: str
    reason: str


def violations(paths: list[str], rules: FactoryCode) -> list[Violation]:
    """Paths an outer-loop change may not touch, with why."""
    found: list[Violation] = []
    for path in paths:
        blocked = next((p for p in rules.never if matches(path, p)), None)
        if blocked is not None:
            found.append(Violation(path, f"matches never-edit rule `{blocked}`"))
        elif not any(matches(path, p) for p in rules.editable):
            found.append(Violation(path, "is not factory code"))
    return found
