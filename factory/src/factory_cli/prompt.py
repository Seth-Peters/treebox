"""Render the first message a worker session receives."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from string import Template

from factory_cli.errors import FactoryError


@dataclass(frozen=True)
class TaskContext:
    slug: str
    branch: str
    base_branch: str
    pr_label: str
    factory_version: str
    factory_sha: str
    session_id: str
    requested_by: str
    source: str
    brief: str


def placeholders(template: str) -> set[str]:
    return {
        m.group("named") or m.group("braced")
        for m in Template.pattern.finditer(template)
        if m.group("named") or m.group("braced")
    }


def unknown_placeholders(template: str) -> set[str]:
    return placeholders(template) - set(TaskContext.__dataclass_fields__)


def render(template: str, context: TaskContext) -> str:
    unknown = unknown_placeholders(template)
    if unknown:
        raise FactoryError(f"task prompt uses unknown placeholders: {sorted(unknown)}")
    return Template(template).substitute(asdict(context))
