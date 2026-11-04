"""Load and validate factory/factory.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from factory_cli.errors import FactoryError

MANIFEST_NAME = "factory.yaml"


@dataclass(frozen=True)
class Resources:
    engineer_agent: str
    reviewer_agent: str
    environment: str
    vault: str


@dataclass(frozen=True)
class SessionDefaults:
    budget_usd: int
    branch_prefix: str
    pr_label: str


@dataclass(frozen=True)
class FactoryCode:
    editable: tuple[str, ...]
    never: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    root: Path  # the directory holding factory.yaml
    name: str
    version: str
    repository_url: str
    base_branch: str
    resources: Resources
    task_prompt: str
    session: SessionDefaults
    factory_code: FactoryCode

    def path(self, relative: str) -> Path:
        return self.root / relative


def _require(data: dict[str, Any], key: str, kind: type, where: str) -> Any:
    value = data.get(key)
    if not isinstance(value, kind):
        raise FactoryError(f"{where}: `{key}` must be a {kind.__name__}")
    return value


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    section: dict[str, Any] = _require(data, key, dict, MANIFEST_NAME)
    return section


def _patterns(data: dict[str, Any], key: str) -> tuple[str, ...]:
    values = _require(data, key, list, f"{MANIFEST_NAME} factory_code")
    if not all(isinstance(v, str) for v in values):
        raise FactoryError(f"{MANIFEST_NAME} factory_code: `{key}` must list strings")
    return tuple(values)


def parse_manifest(data: Any, root: Path) -> Manifest:
    if not isinstance(data, dict):
        raise FactoryError(f"{MANIFEST_NAME} must be a mapping")
    repo = _section(data, "repository")
    res = _section(data, "resources")
    sess = _section(data, "session")
    code = _section(data, "factory_code")
    return Manifest(
        root=root,
        name=_require(data, "name", str, MANIFEST_NAME),
        version=str(_require(data, "version", str, MANIFEST_NAME)),
        repository_url=_require(repo, "url", str, "repository"),
        base_branch=_require(repo, "base_branch", str, "repository"),
        resources=Resources(
            engineer_agent=_require(res, "engineer_agent", str, "resources"),
            reviewer_agent=_require(res, "reviewer_agent", str, "resources"),
            environment=_require(res, "environment", str, "resources"),
            vault=_require(res, "vault", str, "resources"),
        ),
        task_prompt=_require(data, "task_prompt", str, MANIFEST_NAME),
        session=SessionDefaults(
            budget_usd=_require(sess, "budget_usd", int, "session"),
            branch_prefix=_require(sess, "branch_prefix", str, "session"),
            pr_label=_require(sess, "pr_label", str, "session"),
        ),
        factory_code=FactoryCode(
            editable=_patterns(code, "editable"),
            never=_patterns(code, "never"),
        ),
    )


def load_manifest(factory_dir: Path) -> Manifest:
    path = factory_dir / MANIFEST_NAME
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise FactoryError(f"no {MANIFEST_NAME} in {factory_dir}") from exc
    except yaml.YAMLError as exc:
        raise FactoryError(f"{path}: invalid YAML: {exc}") from exc
    return parse_manifest(raw, factory_dir)
