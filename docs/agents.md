# Every agent ships its own cage

Pick any coding agent and it arrives with its *own* answer to two questions:
**how do I run agents in parallel without them clobbering each other's files,
and how do I stop one from touching the wrong thing?** Each tool solves this
with a bespoke config — a different file, a different schema, a different set
of words for the same ideas. Learn one and it teaches you nothing about the
next.

That fragmentation is treebox's reason to exist. treebox owns the *isolation*
layer — one named-worktree layout and one operator-owned sandbox — and
launches your agent of choice inside it. **One config to learn, any agent in
the box.**

## The landscape

Four popular agents, four unrelated ways to spell "sandbox" and "worktree":

| Agent | Sandbox / permission config | Built-in worktrees | Config lives in |
| ----- | --------------------------- | ------------------ | --------------- |
| **Claude Code** | `permissions` allow/ask/deny rules **plus** a native OS sandbox (Seatbelt on macOS, bubblewrap on Linux) **plus** a reference dev container | **Yes** — `--worktree` / `-w` | `.claude/settings.json`, `~/.claude/settings.json`, `.devcontainer/` |
| **OpenAI Codex** (CLI) | `sandbox_mode` × `approval_policy`; OS sandbox is Seatbelt on macOS, Landlock + seccomp on Linux | **No** in the CLI (the Codex *app* creates a worktree per task) | `~/.codex/config.toml`, `--config` flags, `[profiles.*]` |
| **opencode** | `permission` rules per tool (`allow`/`ask`/`deny`, last-match-wins); no OS or container sandbox | **No** — community plugins only | `opencode.json`, `~/.config/opencode/opencode.json` |
| **pi** | No built-in permission system — runs with all permissions by default; isolation delegated to Docker / a micro-VM | **No** in core — via the `pi-subagents` extension | `~/.pi/agent/settings.json`, `.pi/settings.json` |

Every cell is a config format you'd otherwise have to carry in your head, and
none of them ports to the tool in the next column.

## Where treebox fits

treebox doesn't compete with any agent's own permission model — it sits one
layer up and makes the *isolation* uniform, then hands the boxed tree to
whichever agent you launch:

<div class="grid cards" markdown>

- :material-source-branch:{ .lg .middle } **One worktree model**

    ---

    Every agent gets the same layout — one directory per worktree name
    (yours, or a generated petname), cut fresh from `origin/<base>` — no
    matter whether the tool has native worktrees, plugin worktrees, or none.

- :material-cube-outline:{ .lg .middle } **One sandbox definition**

    ---

    A single operator-owned sandbox template sandboxes any agent —
    instead of one tool's Seatbelt profile, another's Landlock policy, and a
    third's "bring your own Docker".

- :material-file-cog:{ .lg .middle } **One config file**

    ---

    `$TREEBOX_CONFIG`, else `$TREEBOX_HOME/config.toml` (default
    `~/.treebox/config.toml`), picks the isolation mode, harness, base, caches,
    and sandbox template. It's the single source of truth, and it's never read
    from the target repo.

- :material-swap-horizontal:{ .lg .middle } **Swap the agent, keep the box**

    ---

    `treebox enter feature/auth --harness claude` and `--harness codex` launch into
    the *same* provisioned, isolated tree. The isolation doesn't change when
    the agent does.

</div>

treebox launches `claude` and `codex` today; the point is that the worktree
and sandbox story is identical across them, and stays identical as more tools
are added. You learn treebox once — not each agent's cage.

!!! note "treebox complements these systems, it doesn't replace them"
    An agent's own permission rules still apply inside the box. treebox's job
    is the layer they all leave to you: a consistent isolated worktree and,
    with `docker` isolation, a consistent sandbox — defined by *your*
    template, [rendered outside the box](how-it-works.md#the-sandbox-config-lives-outside-the-box)
    so the agent can't edit its own cage.

## Sources

The table summarizes each tool's first-party documentation:

- **Claude Code** — [permissions](https://code.claude.com/docs/en/permissions) · [sandboxing](https://code.claude.com/docs/en/sandboxing) · [dev containers](https://code.claude.com/docs/en/devcontainer) · [worktrees](https://code.claude.com/docs/en/worktrees)
- **OpenAI Codex** — [sandbox & approvals](https://developers.openai.com/codex/security) · [config reference](https://developers.openai.com/codex/config-reference) · [app worktrees](https://developers.openai.com/codex/app/worktrees)
- **opencode** — [permissions](https://opencode.ai/docs/permissions/) · [config](https://opencode.ai/docs/config/)
- **pi** — [containerization](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/containerization.md) · [settings](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/settings.md) · [pi-subagents](https://pi.dev/packages/pi-subagents)
