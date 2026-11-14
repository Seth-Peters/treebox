"""`factory check`: is the factory definition coherent, and does a change stay in bounds?"""

from __future__ import annotations

from pathlib import PurePosixPath

from factory_cli.agents import AgentFile, load_agent
from factory_cli.boundary import violations
from factory_cli.errors import FactoryError
from factory_cli.manifest import Manifest

GITHUB_MCP = "github"


def _missing_files(manifest: Manifest) -> list[str]:
    res = manifest.resources
    required = (("environment", res.environment), ("worker vault", res.worker_vault))
    return [
        f"{label} file missing: {rel}"
        for label, rel in required
        if not manifest.path(rel).is_file()
    ]


def _engineer_problems(manifest: Manifest, engineer: AgentFile) -> list[str]:
    problems: list[str] = []
    res = manifest.resources
    engineer_dir = PurePosixPath(res.engineer_agent).parent
    listed = {engineer_dir / p for p in engineer.subagent_paths()}
    if PurePosixPath(res.reviewer_agent) not in listed:
        problems.append(
            f"{res.engineer_agent} does not list the reviewer ({res.reviewer_agent}) "
            "under multiagent.subagents.predefined_agents"
        )
    try:
        engineer.mcp_server_url(GITHUB_MCP)
    except FactoryError as exc:
        problems.append(str(exc))
    return problems


def definition_problems(manifest: Manifest) -> list[str]:
    """Structural problems in the factory's own files. Empty means coherent."""
    problems = _missing_files(manifest)
    res = manifest.resources
    agents: dict[str, AgentFile] = {}
    for label, rel in (("engineer", res.engineer_agent), ("reviewer", res.reviewer_agent)):
        try:
            agents[label] = load_agent(manifest.path(rel))
        except FactoryError as exc:  # report every problem, not just the first
            problems.append(str(exc))
    if "engineer" in agents:
        problems += _engineer_problems(manifest, agents["engineer"])
    return problems


def boundary_problems(manifest: Manifest, changed: list[str]) -> list[str]:
    return [f"{v.path} {v.reason}" for v in violations(changed, manifest.factory_code)]
