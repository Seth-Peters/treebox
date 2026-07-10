# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> `AGENTS.md` symlinks to `CLAUDE.md`, and `.agents/skills/` symlinks to
> `.claude/skills/`. The `treebox` skill's canonical copy lives in `skills/`
> (kept there for users to copy-paste); `.claude/skills/treebox/` symlinks to it.
> Edit the sources — `CLAUDE.md` and `skills/` — and the symlinks reflect the change.

## What this is

`treebox` is a Python CLI that hands AI coding agents isolated, ready-to-run git
worktrees — one directory per worktree *name* (user slug or generated petname),
run either host-native or inside a docker sandbox. It is orchestration glue:
it shells out to `git` and `docker`. Requires **Python 3.11+** (the sandbox
template it provisions is separately pinned to CPython 3.14.6).

## Commands

```bash
uv run treebox ...                      # run the CLI from the working tree
uv run --extra dev python -m pytest     # full unit + integration suite
uv run --extra dev python -m pytest tests/test_units.py::test_name   # single test (or -k <pattern>)
uv run --extra dev ruff check src tests # lint
uv run --extra dev ruff format --check src tests   # format check (drop --check to apply)
uv run --extra dev mypy                 # strict type check (config in pyproject.toml)
uv run --extra dev pre-commit install   # install lint/format/strict type-check hooks (see CONTRIBUTING.md)
scripts/golden-diff.sh                  # diff CLI output against tests/golden snapshots
./scripts/validate.sh                   # full gate: lint + typecheck + shell assets + tests + snapshots + smoke
uv pip install -e ".[dev]"              # editable dev environment
uv run --extra docs mkdocs serve        # docs site (docs/ + mkdocs.yml), live-reloading
uv run --extra docs mkdocs build --strict   # build docs to site/ (gitignored)
```

Run `validate.sh` before changes that affect provisioning, runners, git
handling, shell assets, or CLI output; it includes the golden CLI-output
snapshots and shell asset checks. Use `scripts/golden-diff.sh --update` only
after an intentional output change has been agreed. `pytest` is enough for
smaller changes.

## Architecture

The whole tool is organized around one seam: **provision (always host-side) vs.
run (pluggable)** — with three registries that define everything swappable.

**The three-seam glossary** (each term is one axis, one module, one registry):

- **Harness** — *which agent CLI launches* (`claude`, `codex`).
  `harnesses.py`: the `Harness` dataclass + `HARNESSES` registry.
- **Runner / isolation** — *where it executes* (`host`, `docker`).
  `runners/`: the `Runner` protocol + `RUNNERS` registry.
- **Ecosystem** — *what setup runs* (uv, npm, pnpm, go, cargo).
  `ecosystems.py`: the `Ecosystem` dataclass + `ECOSYSTEMS` registry.

Module map:

- **`provision.py`** owns the host-side half, identical for every runner:
  `fetch → resolve branch → git worktree add → install pre-push guard →
  copy submodules → copy .env → runner.setup (cache-backed) →
  record lockfile hash → hand to runner`. An explicit `create NAME` uses the
  name as the branch, created fresh from `origin/<base>` (an existing branch is
  a `BRANCH_EXISTS` conflict — `--checkout` is the resume path); a nameless
  `create` makes a `treebox/<petname>` placeholder. Every worktree gets the
  pre-push guard (`extensions.worktreeConfig` + `core.hooksPath` into the
  private git dir), which rejects `treebox/*` refs — so placeholders are
  un-pushable until renamed, real branches unaffected.
- **`harnesses.py`** is the one place agent-CLI wiring lives: each `Harness`
  hides its autonomous launch argv, host login dir, staged login files, and
  login advice behind a small method interface; `VALID_HARNESSES` and the
  doctor login rows derive from `HARNESSES`. Boundary values (CLI/TOML/state)
  stay `str` and are resolved to the object once, in `cli.py`.
- **`runners/base.py`** defines the `Runner` protocol — the *only* thing that
  differs between modes — implemented by **`runners/host.py`** (setup + agent in
  the worktree shell) and **`runners/docker.py`** (plain `docker build/run`,
  setup via a baked-in `post-create.sh`, agent via `docker exec`; the worktree
  and its git common dir are bind-mounted at their host paths so in-container
  git just works). `runners/__init__.py` holds the `RUNNERS` registry;
  `get_runner` and `VALID_ISOLATION` derive from it. Doctor-facing vocabulary
  (preflight detail, whether a login is a hard gate) lives in `RunnerFacts`,
  not in the run methods; teardown options (docker's `remove_volumes`) arrive
  at the runner's constructor, never through the protocol.
- **`cli.py`** (Typer) is the entry point: `create [NAME] / enter <ref> /
  list / teardown <ref>... / template <init|list|path> / doctor / version`
  (`ls`/`rm` are hidden aliases of list/teardown, `template ls` of
  `template list`). The `template` sub-app
  scaffolds and inspects operator-owned sandbox templates via `assets.py`'s
  resolver, so customizing a docker sandbox never needs a `python -c` reach
  into the package. `enter`/`teardown` resolve a ref as
  name → live branch → unique substring (`resolve.py`); ambiguity exits 2.
  `create` and `enter` share `_run_session` for runner preflight, the
  per-name lock, provision error classification, and the final
  `--json`/`--print`/launch fork. `_reconcile_with_state` folds recorded
  creation-time choices into existing-worktree sessions before the runner and
  harness objects are resolved: recorded isolation conflicts with a mismatched
  explicit `--isolation`, while recorded firewall/harness/template protect an
  existing worktree from config-default drift.
  It enforces **stable exit codes** (`0` ok · `1` runtime/doctor hard-check ·
  `2` usage · `3` not-found · `4` auth/fetch · `5` conflict) and **`--json`**
  output carrying a `schemaVersion` that only gains fields within a version —
  a breaking reshape bumps it (git-porcelain discipline). Agents
  branch on these, so don't change their meanings casually.
- **`ecosystems.py`** detects package managers (uv, npm, pnpm, go, cargo),
  drives their cache-backed setup, and defines which manifest files feed the
  SHA-256 hash. "Warmth" lives in shared host caches, not the tree.
- **`state.py`** stores per-worktree state (lockfile hash + provisioning
  choices) inside the worktree's private git dir (`.git/worktrees/<id>/`), so it
  never appears in `git status` and is pruned with the worktree. The lockfile
  hash is what lets `enter` re-sync only when deps changed; the recorded
  choices are what let `enter`/`teardown` recover the worktree's created-time
  isolation, firewall, harness, and template defaults.
- **`models.py`** holds the `Worktree` value object and the name-as-identity
  rule: the *name* is the directory leaf and lock key, never renamed; the
  *branch* is a mutable attribute read live from git (the agent renames it
  with `git branch -m`). Branch-shaped inputs (`create feature/auth`,
  `create --checkout feature/auth`) derive the name by flattening slashes to
  `--` (`feature--auth`); generated names come from `names.py`. Also home to
  `expand_user`, the tolerant `~`-expansion helper every path-consuming module
  shares (an unresolvable `~user` stays literal instead of raising).
- **`config.py`** is **user-level TOML only** (`$TREEBOX_CONFIG`, else
  `$TREEBOX_HOME/config.toml`, default `~/.treebox/config.toml`), never read
  from the target repo — a repo-level config could run arbitrary host commands.
  Same principle in **`assets.py`**: templates resolve from
  `$TREEBOX_TEMPLATE_DIR`, then `$TREEBOX_HOME/templates/<name>` (default
  `~/.treebox/templates/<name>`), and the sandbox template is operator-owned
  and rendered into a host-side dir *beside* the worktree, never inside the
  mount, so a boxed agent cannot edit the config that defines its own sandbox.

## Extending treebox

**Adding a harness** (a new agent CLI):

1. One `Harness` entry in `harnesses.py`'s `HARNESSES` registry — name,
   autonomous argv, host login dir, staged login files. If the new CLI's
   credentials are *behavior* rather than plain files (generated settings, a
   non-file auth store), override the relevant methods instead of growing
   branches in callers.
2. Add the harness name to `config.py`'s hard-coded `Harness` Literal alias.
   The drift test `test_registry_vocabularies_cannot_drift` must keep passing:
   the alias, `HARNESSES`, and `VALID_HARNESSES` must name the same set.
3. One operator-template stanza — a `Dockerfile` install line and a
   config-dir env line in `container.json`. This half is deliberately
   template territory (see the `assets.py` security model): treebox never
   derives sandbox contents from code paths the target repo can influence.

Runtime config validation, `--harness` help, doctor login rows, the no-login
advice, credential staging, and sandbox mounts derive from the registry.

**Adding a runner** (a new isolation backend):

1. One `Runner` implementation (see the protocol in `runners/base.py` and
   the contract below).
2. One entry in `runners/__init__.py`'s `RUNNERS` registry — the factory
   declares which generic options the adapter consumes.
3. Add the isolation name to `config.py`'s hard-coded `Isolation` Literal
   alias. The drift test `test_registry_vocabularies_cannot_drift` must keep
   passing: the alias, `RUNNERS`, and `VALID_ISOLATION` must name the same set.

Runtime config validation and the `--isolation` help derive from
`VALID_ISOLATION`, which derives from the registry.

### The isolation contract

The `Runner` seam abstracts **where the agent process runs** — it does *not*
abstract **where the workspace lives**. Host locality binds every backend;
the security invariants bind sandboxed backends. `HostRunner` is the deliberate
non-sandbox exception: it launches directly on the host with live
`~/.claude` / `~/.codex` login dirs and normal host repo access.

What a sandboxed backend must **guarantee** (the security invariants):

- Staged credential *copies* only — the live `~/.claude` / `~/.codex` are
  never exposed to the sandbox (they hold host-executed config).
- The sandbox-defining config is rendered outside the mount, so a boxed
  agent cannot edit the definition of its own box.
- The shared `.git/hooks` is presented read-only (host git executes it).
- Egress lockdown, when enabled, exists before any workspace-derived code
  runs (firewall-before-post-create).
- Only user-level treebox config is ever read — never the target repo's.

What a backend may **assume** (host locality):

- Provisioning already happened host-side (`provision.py` writes the
  worktree, guard, submodules, `.env` before the runner sees it).
- The host filesystem is visible *at identical absolute paths*: docker
  bind-mounts the worktree and its git common dir 1:1, state lives in the
  host-side private git dir, the lockfile hash stats host files, and the
  name-is-permanent rule in `models.py` presumes the absolute-path mount.

Consequence: OCI-compatible engines (podman, etc.) and bind-mount sandboxes
(bubblewrap/nsjail-style) fit this seam as-is. SSH-remote, VM, or cloud
backends — anywhere the agent's filesystem ≠ the host's — would first need a
filesystem-transport seam that deliberately does not exist yet (no adapter
needs path translation today, so building it would be indirection).

## Invariants to preserve

- **Code is never silently stale.** `create` requires a successful
  `git fetch origin` and branches from the fresh `origin/<base>`; a failed fetch
  exits `4` loudly. `--no-fetch` is the only (explicit) escape.
- **Subscription auth only.** Agents launch via `~/.claude` / `~/.codex` logins;
  `ANTHROPIC_API_KEY` is never used.
- **Non-interactive friendly.** Data → stdout, diagnostics → stderr; color and
  spinners degrade when stderr is not a TTY; `--json` / `--print` / `--dry-run`
  exist for scripting and must keep working.
- **Never trust the target repo's config.** No reading its container config,
  its treebox config, or its setup hooks for the sandbox definition.

## Conventions

- Conventional Commit subjects (`feat(...)`, `fix:`, `docs:`); keep commits
  scoped and separate docs-only edits when practical.
- Bundled bash assets (`Dockerfile`, `*.sh`, `container.json`) under
  `src/treebox/assets/` ship as package data and are read via
  `importlib.resources` — edit them in place rather than rewriting in Python.
- Do not commit secrets or generated `.env` files.
