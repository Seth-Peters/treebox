<p align="center">
  <img src="https://raw.githubusercontent.com/Seth-Peters/treebox/main/assets/treebox-logo.png" alt="treebox logo: a small tree growing inside a glass box" width="300">
</p>

<h1 align="center">Spin up worktrees, put them in a box,<br>tear them down - treebox</h1>

<p align="center">
  <strong>Isolated, ready-to-run git worktrees for coding agents.</strong>
</p>

<p align="center">
  <a href="https://github.com/Seth-Peters/treebox/actions/workflows/ci.yml"><img src="https://github.com/Seth-Peters/treebox/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://seth-peters.github.io/treebox/"><img src="https://img.shields.io/badge/docs-seth--peters.github.io%2Ftreebox-2f6f4f" alt="Documentation"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776ab" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-2f6f4f" alt="Platform: macOS and Linux">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-2f6f4f" alt="Apache 2.0 License"></a>
  <a href="https://www.linkedin.com/in/seth-peters/"><img src="https://img.shields.io/badge/LinkedIn-Seth%20Peters-0a66c2?logo=linkedin&logoColor=white" alt="Seth Peters on LinkedIn"></a>
</p>

<p align="center">
  <a href="https://seth-peters.github.io/treebox/">Documentation</a> ·
  <a href="https://seth-peters.github.io/treebox/install/">Install</a> ·
  <a href="https://seth-peters.github.io/treebox/usage/">Usage</a> ·
  <a href="https://seth-peters.github.io/treebox/how-it-works/">How it works</a> ·
  <a href="https://www.linkedin.com/in/seth-peters/">Seth Peters</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/Seth-Peters/treebox/main/assets/treebox-demo.gif" alt="treebox lifecycle demo: create, enter, ls, and an interactive teardown" width="900">
</p>

---

`treebox` hands coding agents isolated, ready-to-run git worktrees — one
directory per worktree name. `treebox create` fetches, cuts a worktree from a
fresh `origin/<base>`, copies your `.env`, installs dependencies from a shared
cache, and launches `claude` or `codex` inside. Name the work up front
(`treebox create fix-auth` works on branch `fix-auth`) or don't: with no name
the worktree gets a stable petname and an un-pushable `treebox/<petname>`
placeholder branch the agent renames when the work takes shape. Agents work the same repo
in parallel without collisions — on a laptop or over plain SSH.

Provisioning is identical everywhere; a pluggable **isolation mode** decides
where the agent runs:

| `--isolation`    | Sandbox   | Agent runs in                                         |
| ---------------- | --------- | ----------------------------------------------------- |
| `host` (default) | none      | the worktree shell                                    |
| `docker`         | sandboxed | a docker container, with your `.env` + caches mounted |

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Seth-Peters/treebox/main/install.sh | sh
```

The script installs with [uv](https://docs.astral.sh/uv/) and stops with
instructions if uv is missing — it never installs a package manager behind
your back. Or install directly:

```bash
uv tool install treebox
```

Host isolation needs `git` plus the selected agent CLI on PATH and logged in.
Docker isolation additionally needs `docker`; the default template supplies the
agent CLI, and treebox copies scoped login files from your `~/.claude` /
`~/.codex` subscription login. See the
[install guide](https://seth-peters.github.io/treebox/install/) for
requirements and installer overrides.

## Usage

The core worktree lifecycle is five commands (`template` scaffolds docker
sandbox templates and `version` prints the version alongside them).

**Check the host.** `doctor` verifies exactly what `create` will need — git,
agent logins, the (optional) `.env`, credentials for the required fetch — and
prints the fix for anything missing:

```bash
treebox doctor
```

**Create.** Fetches `origin`, cuts a worktree from the fresh `origin/main`,
copies `.env` and submodules, syncs dependencies from the shared cache, and
launches the agent:

```bash
treebox create                          # generated name (brave-otter), host-native
treebox create fix-auth                 # named up front: works on branch fix-auth
treebox create feature/auth             # slash names: branch feature/auth, dir feature--auth
treebox create fix-auth --isolation docker   # sandboxed
treebox create --checkout feature/auth       # exact existing branch (resume, PR review)
treebox create auth-fixes --base feature/auth   # stack on any base branch
```

The optional name is the worktree's permanent identity **and** its branch,
created fresh from `origin/<base>` — slug tokens joined by slashes, with the
slashes flattened to `--` in the directory. Omit the name and the branch
starts as a `treebox/<petname>` placeholder — rename it conventionally
(`git branch -m feature/user-auth`, `fix/login-race`, `chore/bump-deps`, …)
when the work has a shape, then push. Every worktree carries a pre-push guard
that keeps `treebox/*` refs **un-pushable**, so a machine-generated name can
never become a PR title. `--checkout` checks out an existing branch exactly;
naming a branch that already exists is a loud conflict pointing there.

`--base` takes any branch, not just `main` — branch off `dev`, or stack a new
worktree on top of an existing PR's branch, even while that branch is checked
out in another worktree. It resolves as the freshly fetched `origin/<base>`,
so push the base first if its latest commits only exist locally.

**Enter.** Come back to an existing worktree. By default it reuses the harness
the worktree was created with; an explicit `--harness` overrides it for that
session only, without changing what's recorded on disk. The ref is the name,
the *current* branch (renames are followed live), or a unique substring of
either. Dependencies re-sync only if the lockfile changed since last time; a
setup that never completed (a prior run died mid-provision) is finished rather
than skipped:

```bash
treebox enter fix-auth --harness claude
treebox enter fix-auth --harness codex -- --resume   # args after -- go to the agent
```

**List.** See what exists, what each worktree was last doing, and what has
gone stale — sorted by recency, with `treebox/*` placeholders flagged
`⚠ unnamed`:

```bash
treebox list
```

**Tear down.** Remove one or more worktrees — and, when you're done, their
branches. Refuses to delete uncommitted work unless forced. Run it with no
refs and treebox walks you through an arrow-key picker, each worktree
annotated with a "will I lose work?" badge (dirty/ahead/merged, plus PR state
when `gh`/`glab` is present):

```bash
treebox teardown fix-auth brave-otter --delete-branch
treebox teardown                        # pick interactively
```

treebox is built to be scripted, including by agents: the worktree commands and
`doctor` take `--json` (data to stdout, diagnostics to stderr, a schema that
only gains fields within a version), `--dry-run` prints the exact commands
without running them (and fails with the same errors a real run would, so it
doubles as a preflight), and exit codes are stable (`0` ok · `1` runtime/doctor
blocked · `2` usage · `3` not found · `4` auth · `5` conflict). Full
reference in the [usage guide](https://seth-peters.github.io/treebox/usage/).

## Every agent ships its own cage

Every coding agent invents its own answer to "run me in parallel" and "don't
let me touch the wrong thing" — a different config file, a different schema, a
different word for the same idea:

| Agent | Sandbox / permission config | Built-in worktrees | Config lives in |
| ----- | --------------------------- | ------------------ | --------------- |
| **Claude Code** | `permissions` allow/ask/deny **+** native OS sandbox (Seatbelt/bubblewrap) **+** dev container | Yes (`--worktree`) | `.claude/settings.json`, `.devcontainer/` |
| **OpenAI Codex** | `sandbox_mode` × `approval_policy` (Seatbelt / Landlock+seccomp) | No in CLI (app only) | `~/.codex/config.toml`, `[profiles.*]` |
| **opencode** | `permission` per-tool allow/ask/deny (no OS sandbox) | No (community plugins) | `opencode.json` |
| **pi** | none built-in ("all permissions by default"); BYO Docker/VM + trust prompt | No in core (`pi-subagents`) | `~/.pi/agent/settings.json` |

Learn one and it teaches you nothing about the next, and none of it ports
across tools. treebox owns the *isolation* layer instead — **one
named-worktree layout, one operator-owned sandbox, one config file** — and
launches your agent of choice inside it. Learn treebox once; swap the agent,
keep the box. Full comparison with citations:
[Agents & sandboxing](https://seth-peters.github.io/treebox/agents/).

## Design

- **Never silently stale.** `create` requires a successful `git fetch origin`
  and branches from the fresh `origin/<base>`; a failed fetch exits `4` loudly.
  `--no-fetch` is the only (explicit) escape.
- **Warmth lives in the cache, not the tree.** Installs hardlink from shared
  caches (`~/.cache/uv`, the pnpm store, …) reused across worktrees and
  containers.
- **The sandbox config lives outside the box.** The container is rendered from
  your operator-owned template beside the worktree, never mounted — a boxed
  agent can't edit its own cage, and the target repo's container config and
  hooks are ignored.
- **Credentials go in as scoped copies.** Only the agents' login files are
  copied into a throwaway per-worktree dir — never the live `~/.claude` /
  `~/.codex` — refreshed on every entry so a host logout or a fresh login
  reaches the sandbox next time; treebox uses your subscription login.

More in [how it works](https://seth-peters.github.io/treebox/how-it-works/).

## Configuration

Optional, and user-level only (`$TREEBOX_CONFIG`, else
`$TREEBOX_HOME/config.toml`, default `~/.treebox/config.toml`) — treebox
never reads config from the target repo:

```toml
isolation = "docker"  # host | docker
harness = "claude"  # claude | codex
base   = "main"
```

All keys, shared-cache overrides, setup hooks, and sandbox templates are
covered in the
[configuration guide](https://seth-peters.github.io/treebox/configuration/).

## Customizing isolation

`docker` isolation builds from a **template you own** — a directory of files
treebox ships and you edit. treebox renders it beside the worktree so a boxed
agent can't touch its own cage. Three steps:

**1. Scaffold a template** — you edit a copy, never the shipped one.
`treebox template init` copies the default into `~/.treebox/templates/<name>`
with the full required file set; a `node` box and a `python` box can coexist,
one per stack:

```bash
treebox template init node          # copies from the built-in default
treebox template list               # names, source, firewall, status, default + what the default bundles (`ls` works too)
treebox template path node          # where it lives: cd "$(treebox template path node)"
```

**2. Edit two files.** The shipped image already bundles Node 22, uv, `gh`,
ripgrep, and the agent CLIs, so most projects touch only these:

```dockerfile
# ~/.treebox/templates/node/Dockerfile — global tooling
USER root
RUN npm install -g pnpm@9 typescript tsx
USER ${USERNAME}
```

```json
// ~/.treebox/templates/node/container.json — your install command
"postCreate": "if [ -f pnpm-lock.yaml ]; then pnpm install --frozen-lockfile; elif [ -f package-lock.json ]; then npm ci; else npm install; fi"
```

(In docker, setup runs `postCreate`, not the host-mode ecosystem auto-detect,
so a non-Python project wires its install here.)

**3. Point treebox at it** — per run with `--isolation docker --template node`,
or set `template = "node"` in `~/.treebox/config.toml`.

Full walkthrough — every file in a template and the `container.json` schema — in
the [configuration guide](https://seth-peters.github.io/treebox/configuration/#customizing-the-sandbox).

## Development

```bash
git clone https://github.com/Seth-Peters/treebox && cd treebox
uv run treebox ...                    # run the CLI from the working tree
uv run --extra dev pre-commit install # lint/format/strict type-check hooks (see CONTRIBUTING.md)
uv run --extra dev python -m pytest   # unit + integration suite
uv run --extra dev mypy               # strict type check
./scripts/validate.sh                 # full gate: lint + typecheck + shell assets + tests + snapshots + smoke
```

## Contributing

Small fixes and docs improvements are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). The roadmap is intentionally light for now: [ROADMAP.md](ROADMAP.md).

## License

[Apache License 2.0](LICENSE)
