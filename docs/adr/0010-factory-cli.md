# Factory CLI

The software factory under `.factory/` is separate from the treebox product:
its own uv project, never shipped in the wheel. Its CLI dispatches a spec to
a Claude Managed Agents session and exports the result. These are the few
non-obvious choices in that CLI, mostly about what dispatch checks and what
it leaves out.

## Decisions

### The MCP bearer token is rotated, not accumulated

Storing a bearer token for an MCP server first lists the vault's credentials
and archives every earlier one for the same server URL, then creates the new
credential. A vault therefore holds one live token per server instead of a
growing pile of stale ones.

Where: `.factory/src/factory_cli/anthropic_api.py` ·
`AnthropicApi.set_mcp_bearer`

### The definition check reports every problem

When loading the agent definitions, a `FactoryError` from one file is
collected and the check continues with the next. The caller gets every
problem in one run, not just the first.

Where: `.factory/src/factory_cli/check.py` · `definition_problems`

### API clients are built through swappable factory functions

The CLI obtains its GitHub and sessions clients through two module-level
functions. They exist as seams for tests, which replace them with fakes.

Where: `.factory/src/factory_cli/cli.py` · `make_github`, `make_sessions`

### The lockfile is not one of the worker's behavior files

Dispatch requires the files the worker's behavior depends on to match
origin. The factory lockfile is deliberately absent from that list: dispatch
reads it locally and the worker never does, so its state on origin does not
affect the worker.

Where: `.factory/src/factory_cli/delegate.py` · `behavior_pathspecs`

### Transcript export skips the primary thread

The sessions API lists the primary thread (the one with no
`parent_thread_id`) together with the subagent threads, and the primary
thread repeats the session's own events. Those events are already written as
`events.jsonl`, so export skips the primary thread and writes only subagent
threads under `threads/`. The test fake returns the primary thread too, to
match the real API and keep that skip covered.

Where: `.factory/src/factory_cli/transcript.py` · `export`;
`.factory/tests/test_factory_cli.py` · `FakeApi.threads`
