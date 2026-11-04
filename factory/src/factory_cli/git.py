"""The few git facts the factory needs, via the git CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory_cli.errors import FactoryError


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise FactoryError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise FactoryError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return done.stdout


def repo_root(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel").strip())


def fetch(repo: Path, branch: str) -> None:
    _git(repo, "fetch", "--quiet", "origin", branch)


def rev_parse(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).strip()


def changed_paths(repo: Path, base: str, pathspecs: tuple[str, ...] = ()) -> list[str]:
    """Paths differing between `base` and the working tree, untracked files included."""
    spec = ["--", *pathspecs] if pathspecs else []
    tracked = _git(repo, "diff", "--name-only", base, *spec).splitlines()
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", *spec).splitlines()
    return sorted({p for p in [*tracked, *untracked] if p})
