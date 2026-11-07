"""The only module that talks to the Anthropic SDK (Managed Agents beta).

Authentication follows the SDK's normal chain: `ANTHROPIC_API_KEY` if set,
otherwise the profile `ant auth login` wrote under ~/.config/anthropic/.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, Literal, TypeVar, cast

import anthropic
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)
from anthropic.types.beta.vaults.beta_managed_agents_static_bearer_create_params import (
    BetaManagedAgentsStaticBearerCreateParams,
)

from factory_cli.dispatch import SessionInfo
from factory_cli.errors import FactoryError

T = TypeVar("T")
Status = Literal["rescheduling", "running", "idle", "terminated"]


def _call(action: str, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except anthropic.APIError as exc:
        raise FactoryError(f"{action} failed: {exc}") from exc
    except anthropic.AnthropicError as exc:  # e.g. no credentials found
        raise FactoryError(f"{action} failed: {exc} (run `ant auth login`)") from exc


def _dump(model: Any) -> dict[str, Any]:
    return cast(dict[str, Any], model.model_dump(mode="json"))


class AnthropicApi:
    """Implements `dispatch.SessionsApi` and the vault call. The SDK sets the beta header."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client_instance = client

    @property
    def _client(self) -> anthropic.Anthropic:
        if self._client_instance is None:
            self._client_instance = _call("loading Anthropic credentials", anthropic.Anthropic)
        return self._client_instance

    def create_session(self, body: dict[str, Any]) -> str:
        session = _call("creating the session", lambda: self._client.beta.sessions.create(**body))
        return str(session.id)

    def send_message(self, session_id: str, text: str) -> None:
        message: BetaManagedAgentsUserMessageEventParams = {
            "type": "user.message",
            "content": [{"type": "text", "text": text}],
        }
        _call(
            f"sending a message to {session_id}",
            lambda: self._client.beta.sessions.events.send(session_id, events=[message]),
        )

    def list_sessions(self, agent_id: str, statuses: tuple[str, ...] = ()) -> list[SessionInfo]:
        wanted = cast(list[Status], list(statuses))

        def fetch() -> list[SessionInfo]:
            page = (
                self._client.beta.sessions.list(agent_id=agent_id, order="desc", statuses=wanted)
                if wanted
                else self._client.beta.sessions.list(agent_id=agent_id, order="desc")
            )
            return [
                SessionInfo(
                    id=s.id,
                    status=s.status,
                    created_at=s.created_at.isoformat(),
                    title=s.title or "",
                    metadata=dict(s.metadata),
                )
                for s in page
            ]

        return _call("listing sessions", fetch)

    def retrieve(self, session_id: str) -> dict[str, Any]:
        return _call(
            f"reading {session_id}",
            lambda: _dump(self._client.beta.sessions.retrieve(session_id)),
        )

    def events(self, session_id: str) -> Iterator[dict[str, Any]]:
        page = _call(
            f"reading events of {session_id}",
            lambda: self._client.beta.sessions.events.list(session_id, order="asc"),
        )
        for event in page:
            yield _dump(event)

    def threads(self, session_id: str) -> list[dict[str, Any]]:
        return _call(
            f"listing threads of {session_id}",
            lambda: [_dump(t) for t in self._client.beta.sessions.threads.list(session_id)],
        )

    def thread_events(self, session_id: str, thread_id: str) -> Iterator[dict[str, Any]]:
        page = _call(
            f"reading thread {thread_id}",
            lambda: self._client.beta.sessions.threads.events.list(
                thread_id, session_id=session_id
            ),
        )
        for event in page:
            yield _dump(event)

    def set_mcp_bearer(self, vault_id: str, mcp_server_url: str, token: str, label: str) -> str:
        auth: BetaManagedAgentsStaticBearerCreateParams = {
            "type": "static_bearer",
            "mcp_server_url": mcp_server_url,
            "token": token,
        }
        credentials = self._client.beta.vaults.credentials

        def stale() -> list[str]:
            return [
                c.id
                for c in credentials.list(vault_id)
                if getattr(c.auth, "mcp_server_url", None) == mcp_server_url
            ]

        def retire(credential_id: str) -> None:
            credentials.archive(credential_id, vault_id=vault_id)

        # Rotation, not accumulation: retire any earlier token for this server.
        for credential_id in _call("listing vault credentials", stale):
            _call("archiving the old credential", partial(retire, credential_id))
        credential = _call(
            "storing the credential",
            lambda: credentials.create(vault_id=vault_id, display_name=label, auth=auth),
        )
        return str(credential.id)
