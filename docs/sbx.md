# sbx isolation (Docker Sandboxes)

`--isolation sbx` runs the agent in a [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)
(`sbx`) **microVM**: one VM per worktree, each with its own kernel and private
Docker daemon. With `docker` isolation on macOS every container shares Docker
Desktop's single Linux VM; with `sbx` they do not.

```bash
brew install docker/tap/sbx && sbx login      # one-time
treebox create fix-auth --isolation sbx
```

## What you need to know

- **Per-agent microVM.** Apple silicon + macOS Sonoma 14+ (or Linux). `sbx` is
  free but **not open source**.
- **`sbx login` is required** (a Docker account). `sbx` also owns the agent's
  login: the agent starts signed in, using the Anthropic login `sbx` manages
  (`sbx secret ls` shows it). Only if that login is missing or expired do you
  need to run `/login` inside the sandbox. treebox stages no credentials for
  this runner, never copies your `~/.claude`, and never reads an API key.
- **No dependencies are installed on the host.** macOS builds would not run in
  the Linux guest, so `create` only makes the sandbox; install dependencies
  inside it. `setup_hook` and the shared host caches do not apply.
- **Git works inside the sandbox.** The worktree and the repo's git dir are
  mounted at their host paths, with `hooks/` mounted read-only — the same
  trade-off as docker isolation. The agent can still rewrite refs and git
  config, so treat the repo's `.git` as attacker-influenced after a sandboxed
  session and prefer treebox's own commands over ad-hoc host git.
- **`--firewall` is not supported** and exits `2`: `sbx` has one global network
  policy, managed with `sbx policy`. `--template` is rejected too; templates are
  a docker-runner feature.
- **No GPU / Metal** inside the VM on macOS.
- CPU and memory are fixed when the sandbox is created. To change them,
  `treebox teardown` and `create` again.

## Configuration

```toml
isolation  = "sbx"
sbx_cpus   = 8        # optional; passed to `sbx create --cpus`
sbx_memory = "16g"    # optional; passed to `sbx create --memory`
```

Both keys are user-level config only, like everything else in treebox. For a
single run, pass `--sbx-cpus N` / `--sbx-memory SIZE` to `create`; a flag wins
over the config value, and using either with another isolation mode is a usage
error (exit `2`) rather than being silently ignored. The config keys themselves
are inert for `host` and `docker`.

```bash
treebox create fix-auth --isolation sbx --sbx-cpus 8 --sbx-memory 16g
```

## How it works

| Stage         | What treebox runs                                                         |
| ------------- | ------------------------------------------------------------------------- |
| `create`      | `sbx create --name <sandbox> [--cpus N] [--memory SIZE] <harness> <worktree> <git dir> <git dir>/hooks:ro` |
| `enter`       | `sbx run --name <sandbox>` (restarts a stopped sandbox itself)            |
| `teardown`    | `sbx rm --force <sandbox>`, best-effort                                   |

The sandbox is named `treebox-<name>-<hash>`, derived from the worktree path so
it is unique across repos. Its name is recorded in
`<worktree-root>/.containers/<worktree>/treebox-sbx.json`, outside every
sandbox mount. `teardown` removes only the sandbox that file names, and only
while that sandbox's primary workspace is still this worktree.

The agent (`claude` or `codex`) is chosen when the sandbox is created; `enter`
reuses the harness the worktree was created with.

## Phase 0 findings

Verified against `sbx` v0.47.0 on macOS 26.6.2 (arm64).

| Question | Result |
| -------- | ------ |
| Workspace mounted at the identical absolute path? | **Yes** — `pwd`, `ls` and the worktree's `.git` pointer file match the host, so no filesystem-transport seam is needed. |
| `--cpus` / `--memory` on `sbx create`? | Yes. `--cpus 0` is auto; memory minimum is 512m. |
| Git common dir writable with `hooks/` read-only nested inside? | Yes — writing `hooks/` fails with "Read-only file system"; `refs/` stays writable. |
| Does git work inside? | Yes — `git status` / `git commit` work, and the commit is visible on the host. |
| Machine-readable listing? | `sbx ls --json` → `{"sandboxes": [{name, id, agent, status, workspaces[], …}]}`; `status` is `running` or `stopped`. |
| Does `sbx rm` prompt? | Yes; `--force` skips it. A missing sandbox exits `1` with `sandbox '…' not found`. |
| Non-interactive login check? | None found (`sbx login` has no status mode). `doctor` reports login state as not checkable. |
| Stop / restart? | `sbx stop` preserves state; `sbx run --name` and `sbx exec` restart a stopped sandbox. |

Not used in this version: `sbx create --clone` (an in-container clone of the
repo, with commits returned over a git remote) would avoid mounting `.git`
writable and is a candidate follow-up.
