"""GitHub issues via the developer's own `gh` login (REST only).

The issue is the task record: the brief is its body, the worker's PR closes
it, and run updates are comments on it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from factory_cli.errors import FactoryError


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    author: str
    url: str
    labels: tuple[str, ...]


class GitHubApi(Protocol):
    def login(self) -> str: ...

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None: ...

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> Issue: ...

    def get_issue(self, repo: str, number: int) -> Issue: ...

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None: ...

    def comment(self, repo: str, number: int, body: str) -> None: ...


def _issue(data: dict[str, Any]) -> Issue:
    return Issue(
        number=int(data["number"]),
        title=str(data["title"]),
        body=str(data.get("body") or ""),
        author=str(data["user"]["login"]),
        url=str(data["html_url"]),
        labels=tuple(str(label["name"]) for label in data.get("labels", [])),
    )


class GhCli:
    """Implements `GitHubApi` with `gh api` (REST), using the caller's gh auth."""

    def _api(self, path: str, method: str = "GET", payload: Any = None) -> Any:
        args = ["gh", "api", path, "--method", method]
        if payload is not None:
            args += ["--input", "-"]
        try:
            done = subprocess.run(
                args,
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise FactoryError("gh is not installed; see https://cli.github.com") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout).strip()
            raise FactoryError(f"gh api {method} {path} failed: {detail}") from exc
        return json.loads(done.stdout) if done.stdout.strip() else None

    def login(self) -> str:
        return str(self._api("user")["login"])

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None:
        try:
            self._api(f"repos/{repo}/labels/{name}")
        except FactoryError:
            payload = {"name": name, "color": color, "description": description}
            self._api(f"repos/{repo}/labels", "POST", payload)

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> Issue:
        payload = {"title": title, "body": body, "labels": labels}
        return _issue(self._api(f"repos/{repo}/issues", "POST", payload))

    def get_issue(self, repo: str, number: int) -> Issue:
        return _issue(self._api(f"repos/{repo}/issues/{number}"))

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._api(f"repos/{repo}/issues/{number}/labels", "POST", {"labels": labels})

    def comment(self, repo: str, number: int, body: str) -> None:
        self._api(f"repos/{repo}/issues/{number}/comments", "POST", {"body": body})
