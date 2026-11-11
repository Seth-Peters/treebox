"""Export a session's full event history, subagent threads included.

This is the raw material for the outer loop: every message, tool call, and
tool result, in order, exactly as the API recorded it.

The session's own event stream is the primary (coordinator) thread, so the
threads listing repeats it. We write it once, as events.jsonl, and write only
the subagent threads (those with a parent thread) under threads/.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_cli.dispatch import SessionsApi

TOOL_USE_TYPES = ("agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use")


@dataclass
class StreamStats:
    """Counts for one event stream: the main thread or one subagent thread."""

    events: int = 0
    tool_calls: int = 0
    model_requests: int = 0
    event_types: Counter[str] = field(default_factory=Counter)
    tools: Counter[str] = field(default_factory=Counter)

    def add(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type", "?"))
        self.events += 1
        self.event_types[kind] += 1
        if kind == "span.model_request_start":
            self.model_requests += 1
        if kind in TOOL_USE_TYPES:
            self.tool_calls += 1
            self.tools[_tool_name(event)] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "tool_calls": self.tool_calls,
            "model_requests": self.model_requests,
            "tools": dict(self.tools.most_common()),
            "event_types": dict(self.event_types.most_common()),
        }


@dataclass
class TranscriptSummary:
    session_id: str
    status: str
    list_cost_usd: str
    active_seconds: float | None
    main: StreamStats = field(default_factory=StreamStats)
    subagents: dict[str, StreamStats] = field(default_factory=dict)  # thread id -> stats

    @property
    def tool_calls(self) -> int:
        return self.main.tool_calls + sum(s.tool_calls for s in self.subagents.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "list_cost_usd": self.list_cost_usd,
            "active_seconds": self.active_seconds,
            "tool_calls_total": self.tool_calls,
            "main": self.main.as_dict(),
            "subagents": {tid: s.as_dict() for tid, s in self.subagents.items()},
        }


def _tool_name(event: dict[str, Any]) -> str:
    name = event.get("name") or event.get("tool_name") or "?"
    server = event.get("mcp_server_name")
    return f"{server}:{name}" if server else str(name)


def _write_jsonl(path: Path, events: Iterable[dict[str, Any]], stats: StreamStats) -> None:
    with path.open("w") as out:
        for event in events:
            out.write(json.dumps(event) + "\n")
            stats.add(event)


def _usd(minor_units: Any) -> str:
    try:
        return f"{int(minor_units) / 100:.2f}"
    except (TypeError, ValueError):
        return "?"


def export(api: SessionsApi, session_id: str, out_dir: Path) -> TranscriptSummary:
    """Write session.json, events.jsonl, threads/<id>.jsonl, and summary.json."""
    target = out_dir / session_id
    (target / "threads").mkdir(parents=True, exist_ok=True)
    session = api.retrieve(session_id)
    (target / "session.json").write_text(json.dumps(session, indent=2) + "\n")
    usage = session.get("usage") or {}
    stats = session.get("stats") or {}
    summary = TranscriptSummary(
        session_id=session_id,
        status=str(session.get("status", "?")),
        list_cost_usd=_usd((usage.get("list_cost") or {}).get("amount")),
        active_seconds=stats.get("active_seconds"),
    )
    _write_jsonl(target / "events.jsonl", api.events(session_id), summary.main)
    for thread in api.threads(session_id):
        if thread.get("parent_thread_id") is None:
            continue  # the primary thread: already written as events.jsonl
        thread_id = str(thread["id"])
        summary.subagents[thread_id] = StreamStats()
        _write_jsonl(
            target / "threads" / f"{thread_id}.jsonl",
            api.thread_events(session_id, thread_id),
            summary.subagents[thread_id],
        )
    (target / "summary.json").write_text(json.dumps(summary.as_dict(), indent=2) + "\n")
    return summary
