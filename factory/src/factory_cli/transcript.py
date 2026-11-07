"""Export a session's full event history, subagent threads included.

This is the raw material for the outer loop: every message, tool call, and
tool result, in order, exactly as the API recorded it.
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
class TranscriptSummary:
    session_id: str
    status: str
    events: int = 0
    tool_calls: int = 0
    event_types: Counter[str] = field(default_factory=Counter)
    tools: Counter[str] = field(default_factory=Counter)
    threads: dict[str, int] = field(default_factory=dict)  # thread id -> event count

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "events": self.events,
            "tool_calls": self.tool_calls,
            "event_types": dict(self.event_types.most_common()),
            "tools": dict(self.tools.most_common()),
            "threads": self.threads,
        }


def _tool_name(event: dict[str, Any]) -> str:
    name = event.get("name") or event.get("tool_name") or "?"
    server = event.get("mcp_server_name")
    return f"{server}:{name}" if server else str(name)


def _write_jsonl(path: Path, events: Iterable[dict[str, Any]], summary: TranscriptSummary) -> int:
    count = 0
    with path.open("w") as out:
        for event in events:
            out.write(json.dumps(event) + "\n")
            count += 1
            kind = str(event.get("type", "?"))
            summary.event_types[kind] += 1
            if kind in TOOL_USE_TYPES:
                summary.tool_calls += 1
                summary.tools[_tool_name(event)] += 1
    return count


def export(api: SessionsApi, session_id: str, out_dir: Path) -> TranscriptSummary:
    """Write session.json, events.jsonl, and threads/<id>.jsonl under out_dir/<session_id>."""
    target = out_dir / session_id
    (target / "threads").mkdir(parents=True, exist_ok=True)
    session = api.retrieve(session_id)
    (target / "session.json").write_text(json.dumps(session, indent=2) + "\n")
    summary = TranscriptSummary(session_id=session_id, status=str(session.get("status", "?")))
    summary.events = _write_jsonl(target / "events.jsonl", api.events(session_id), summary)
    for thread in api.threads(session_id):
        thread_id = str(thread["id"])
        path = target / "threads" / f"{thread_id}.jsonl"
        summary.threads[thread_id] = _write_jsonl(
            path, api.thread_events(session_id, thread_id), summary
        )
    (target / "summary.json").write_text(json.dumps(summary.as_dict(), indent=2) + "\n")
    return summary
