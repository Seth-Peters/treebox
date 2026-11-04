"""The only module that talks to the Anthropic SDK (Managed Agents beta)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import anthropic
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)
from anthropic.types.beta.vaults.beta_managed_agents_static_bearer_create_params import (
    BetaManagedAgentsStaticBearerCreateParams,
)

from factory_cli.errors import FactoryError

T = TypeVar("T")


def _call(action: str, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except anthropic.APIError as exc:
        raise FactoryError(f"{action} failed: {exc}") from exc


class AnthropicApi:
    """Implements `dispatch.SessionsApi` and the vault call. The SDK sets the beta header."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client or anthropic.Anthropic()

    def create_session(self, body: dict[str, Any]) -> str:
        session = _call("creating the session", lambda: self._client.beta.sessions.create(**body))
        return str(session.id)

    def send_message(self, session_id: str, text: str) -> None:
        message: BetaManagedAgentsUserMessageEventParams = {
            "type": "user.message",
            "content": [{"type": "text", "text": text}],
        }
        _call(
            f"sending the task to {session_id}",
            lambda: self._client.beta.sessions.events.send(session_id, events=[message]),
        )

    def set_mcp_bearer(self, vault_id: str, mcp_server_url: str, token: str, label: str) -> str:
        auth: BetaManagedAgentsStaticBearerCreateParams = {
            "type": "static_bearer",
            "mcp_server_url": mcp_server_url,
            "token": token,
        }
        credential = _call(
            "storing the credential",
            lambda: self._client.beta.vaults.credentials.create(
                vault_id=vault_id, display_name=label, auth=auth
            ),
        )
        return str(credential.id)
