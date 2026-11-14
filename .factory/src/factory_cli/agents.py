"""Read agent definition files (Markdown: YAML front matter + system prompt)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from factory_cli.errors import FactoryError


@dataclass(frozen=True)
class AgentFile:
    path: Path
    config: dict[str, Any]
    system_prompt: str

    def mcp_server_url(self, name: str) -> str:
        for server in self.config.get("mcp_servers") or []:
            if isinstance(server, dict) and server.get("name") == name:
                url = server.get("url")
                if isinstance(url, str):
                    return url
        raise FactoryError(f"{self.path}: no MCP server named `{name}`")

    def subagent_paths(self) -> list[str]:
        multiagent = self.config.get("multiagent") or {}
        subagents = multiagent.get("subagents") or {} if isinstance(multiagent, dict) else {}
        listed = subagents.get("predefined_agents") or [] if isinstance(subagents, dict) else []
        return [entry for entry in listed if isinstance(entry, str)]


def parse_agent(text: str, path: Path) -> AgentFile:
    if not text.startswith("---\n"):
        raise FactoryError(f"{path}: missing YAML front matter")
    try:
        _, front, body = text.split("---\n", 2)
    except ValueError as exc:
        raise FactoryError(f"{path}: unterminated front matter") from exc
    try:
        config = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        raise FactoryError(f"{path}: invalid front matter: {exc}") from exc
    if not isinstance(config, dict):
        raise FactoryError(f"{path}: front matter must be a mapping")
    for key in ("name", "model"):
        if key not in config:
            raise FactoryError(f"{path}: front matter needs `{key}`")
    if not body.strip():
        raise FactoryError(f"{path}: empty system prompt")
    return AgentFile(path=path, config=config, system_prompt=body.strip())


def load_agent(path: Path) -> AgentFile:
    try:
        return parse_agent(path.read_text(), path)
    except FileNotFoundError as exc:
        raise FactoryError(f"agent file not found: {path}") from exc
