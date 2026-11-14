"""The e2e feature map, the scenario prose and the scenario code stay in step.

Fast and deterministic (no CLI runs, no docker): this is the part of the
end-to-end suite that ``validate.sh`` gates on. It derives the user-visible
routes from the source (Typer commands and options, ``--json`` error codes,
config keys, environment variables) and fails when the feature map in
``.claude/skills/treebox-e2e/references/feature-map.md`` misses one, keeps a
stale one, points at a source symbol that does not exist, or names a scenario
that ``scenarios.md`` and ``tests/e2e/scenarios.py`` do not both define.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import typer
from test_contract import _emitted_error_codes

from treebox.cli import app
from treebox.config import USER_KEYS

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "treebox"
REFS = ROOT / ".claude" / "skills" / "treebox-e2e" / "references"
FEATURE_MAP = REFS / "feature-map.md"
SCENARIOS_MD = REFS / "scenarios.md"

sys.path.insert(0, str(ROOT / "tests" / "e2e"))
import harness  # noqa: E402
import scenarios  # noqa: E402, F401  (registers the scenarios)

INVARIANT_IDS = {"X1", "X2", "X3", "X4", "X5"}


@dataclasses.dataclass(frozen=True)
class Row:
    key: str
    sources: tuple[str, ...]
    scenarios: tuple[str, ...]


def map_rows() -> list[Row]:
    rows: list[Row] = []
    for line in FEATURE_MAP.read_text().splitlines():
        match = re.match(r"^\| `([^`]+)` \| (.+?) \| (.+?) \|$", line)
        if not match:
            continue
        sources = tuple(re.findall(r"`([^`]+)`", match.group(2)))
        ids = tuple(s.strip() for s in match.group(3).split(","))
        rows.append(Row(match.group(1), sources, ids))
    return rows


def _walk(command: Any, path: list[str]) -> Iterator[tuple[list[str], Any]]:
    yield path, command
    for name, sub in (getattr(command, "commands", None) or {}).items():
        yield from _walk(sub, [*path, name])


def cli_routes() -> set[str]:
    """`cmd` for every command and `cmd --opt` for every option of a visible
    command; hidden aliases are one route each (their options are the
    original command's)."""
    routes: set[str] = set()
    for path, command in _walk(typer.main.get_command(app), []):
        prefix = " ".join(path)
        if path:
            routes.add(prefix)
        if getattr(command, "hidden", False):
            continue
        for param in command.params:
            longs = [o for o in param.opts if o.startswith("--")]
            if longs:
                routes.add(f"{prefix} {longs[0]}".strip())
    return routes


def _source_trees() -> Iterator[ast.AST]:
    for path in SRC.rglob("*.py"):
        yield ast.parse(path.read_text(), filename=str(path))


def error_routes() -> set[str]:
    """Every stable `error.code` the source can emit: the same syntax-tree
    collector that keeps the error-code table in docs/usage.md complete
    (tests/test_contract.py), so the two drift checks cannot disagree."""
    return {f"error {c}" for c in _emitted_error_codes()}


def config_routes() -> set[str]:
    """The keys a user config may set (``config.USER_KEYS``); the other
    Config fields are internal and no route."""
    return {f"config {key}" for key in USER_KEYS}


def env_routes() -> set[str]:
    """Variables treebox reads (``os.environ.get("X")``) and every
    ``TREEBOX_*`` name it reads or exports (a Typer ``envvar``, the worktree
    identity it gives setup and the agent)."""
    names: set[str] = set()
    for tree in _source_trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and ast.unparse(node.func.value) == "os.environ"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                names.add(str(node.args[0].value))
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"TREEBOX_[A-Z_]+", node.value)
            ):
                names.add(node.value)
    return {f"env {n}" for n in names}


def derived_routes() -> set[str]:
    return cli_routes() | error_routes() | config_routes() | env_routes()


def md_scenario_ids() -> list[str]:
    return re.findall(r"^- \[ \] \*\*([A-Z]+\d+[a-z]?)\b", SCENARIOS_MD.read_text(), re.M)


def code_scenario_ids() -> list[str]:
    return [s.id for s in harness.SCENARIOS]


def symbol_exists(ref: str) -> bool:
    file_part, _, symbol = ref.partition("::")
    path = SRC / file_part
    if not path.is_file() or not symbol:
        return False
    scope: list[ast.stmt] = ast.parse(path.read_text()).body
    for part in symbol.split("."):
        found: ast.stmt | None = None
        for node in scope:
            names: list[str] = []
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            if part in names:
                found = node
                break
        if found is None:
            return False
        scope = found.body if isinstance(found, ast.ClassDef) else []
    return True


def test_every_derived_route_has_a_row() -> None:
    keys = {row.key for row in map_rows()}
    missing = sorted(derived_routes() - keys)
    assert not missing, f"add these routes to {FEATURE_MAP.name}: {missing}"


def test_no_stale_rows() -> None:
    derived = derived_routes()
    stale = [
        row.key
        for row in map_rows()
        if not row.key.startswith("behavior ") and row.key not in derived
    ]
    assert not stale, f"these rows name routes the source no longer has: {stale}"


def test_rows_are_unique() -> None:
    keys = [row.key for row in map_rows()]
    assert len(keys) == len(set(keys))


def test_source_references_exist() -> None:
    bad = [(row.key, ref) for row in map_rows() for ref in row.sources if not symbol_exists(ref)]
    assert not bad, f"unknown source references: {bad}"
    assert all(row.sources for row in map_rows()), "every row names its source"


def test_scenario_ids_agree_between_prose_and_code() -> None:
    md = md_scenario_ids()
    code = code_scenario_ids()
    assert len(md) == len(set(md)), "duplicate ids in scenarios.md"
    assert len(code) == len(set(code)), "duplicate ids in scenarios.py"
    assert set(md) - INVARIANT_IDS == set(code), (
        f"only in scenarios.md: {sorted(set(md) - set(code) - INVARIANT_IDS)}; "
        f"only in scenarios.py: {sorted(set(code) - set(md))}"
    )
    assert set(md) >= INVARIANT_IDS


def test_every_scenario_is_mapped_and_every_mapped_id_exists() -> None:
    known = set(md_scenario_ids())
    mapped = {sid for row in map_rows() for sid in row.scenarios}
    assert not mapped - known, f"rows name unknown scenarios: {sorted(mapped - known)}"
    assert not known - mapped, f"scenarios no row names: {sorted(known - mapped)}"


def test_docker_scenarios_use_the_e2e_prefix() -> None:
    """Every docker worktree name in the scenario code carries the reaper's
    prefix, so a crashed run's containers can always be reaped."""
    text = (ROOT / "tests" / "e2e" / "scenarios.py").read_text()
    names = re.findall(r'"(e2e-[a-z0-9-]+|[a-z0-9-]+)",\s*\*docker_args', text)
    assert names and all(n.startswith("e2e-") for n in names), names


@pytest.mark.parametrize(
    "route",
    [
        "create --checkout",
        "error LOCK_HELD",
        "config caches",
        "env TREEBOX_NON_INTERACTIVE",
        "env TREEBOX_PORT_BASE",
    ],
)
def test_derivation_sees_known_routes(route: str) -> None:
    assert route in derived_routes()
