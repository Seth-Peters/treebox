---
name: treebox
description: Create, enter, list, and tear down isolated git worktree sandboxes (host-native or docker-backed) through the treebox CLI. Use when the user asks to create a worktree, spin up an agent sandbox, enter a sandbox, list sandbox worktrees, or tear one down.
---

# Treebox

Use the `treebox` CLI for all mechanics. Do not run `git worktree add`, `git worktree remove`, or Docker cleanup commands manually unless diagnosing a CLI failure.

A pluggable **isolation mode** decides where the agent runs:
- `--isolation host` (default): provisions the worktree and launches the agent directly on the host, in the worktree dir. No container, no Docker — the right default on an SSH box or a trusted machine.
- `--isolation docker`: same provisioning, but launches the agent inside a docker container with the shared caches and scoped copies of the `~/.claude`/`~/.codex` login files mounted in. Needs only `docker`.

Treebox itself requires Python 3.11 or newer. The bundled sandbox template is pinned to CPython 3.14.6.

Provisioning (fetch → resolve branch → worktree add → copy submodules → copy `.env` → cache-backed setup → record lockfile hash) is identical for both isolation modes; only the launch seam differs.

## Workflow

1. Run `treebox doctor` (add `--isolation docker` to check the Docker daemon when docker isolation will be used).
2. If creating named work, ask for a short valid worktree name: lowercase slug tokens (`[a-z0-9-]+`) optionally joined by slashes (`fix-auth`, `feature/user-auth`). The name is used directly as the branch name (never the reserved `treebox/` prefix); slashes flatten to `--` in the directory. Omit it only when the user wants treebox to generate a petname on a guarded `treebox/` placeholder branch.
3. Create with `treebox create <name> --base <trunk>`, or use `treebox create [name] --checkout <existing-branch>` to resume/review an existing branch.
4. If any command exits non-zero, stop and show the full error (and its `hint`). Do not retry automatically — branch on the exit code (below).
5. Report that the CLI launched the agent, or show the error that prevented launch.

## Command reference

- `create [NAME]` — provision and launch. Flags: `--base <branch>` `--checkout <existing-branch>` `--isolation host|docker` `--harness claude|codex` `--template <name>` `--cold` `--no-fetch` `--firewall/--no-firewall` `--dry-run` `--print` `--json` `--repo <path>` `--root <dir>` `--quiet` `--verbose`.
- `enter REF [-- AGENT_ARGS…]` — re-launch in an existing worktree; refreshes `.env`, reuses recorded isolation/firewall/harness/template defaults, and re-syncs deps only if the lockfile changed. Flags: `--isolation` `--harness` `--template <name>` `--cold` `--print` `--json` `--repo` `--root` `--quiet` `--verbose`.
- `list` (alias `ls`) — table of worktrees (branch · isolation · deps freshness · `.env` presence · directory). Flags: `--repo` `--root` `--json`.
- `teardown [REF...]` (alias `rm`) — remove selected worktrees; caches are left intact. Flags: `--force` `--delete-branch` `--remove-volumes` `--skip-container` `--json` `--isolation` `--repo` `--root` `--quiet` `--verbose`.
- `template init NAME` — scaffold an operator-owned sandbox template into `$TREEBOX_HOME/templates/<name>` (copies the full required file set from the default). Flags: `--from <template>` `--force` `--json`. Then edit its `Dockerfile`/`container.json` and select with `--template <name>`.
- `template list` (alias `ls`) — table of templates (name · source · status) with the configured default flagged; when `default` still resolves to the bundled image, also highlights what that image ships. Flags: `--json`.
- `template path [NAME]` — print a template's resolved directory (for scripting: `cd "$(treebox template path <name>)"`).
- `doctor` — check git, login credentials, UID/GID, and isolation deps. Flags: `--isolation` `--repo` `--json`.
- `version` / `--version` / `-V` — print the version.

## Examples

```bash
treebox create fix-auth --base dev          # new branch fix-auth from origin/dev, host isolation
treebox create docs-fix --base dev --isolation docker
treebox create hotfix-login --base main --cold  # ignore shared cache, from-source build
treebox create fix-auth --harness codex        # Codex instead of Claude
treebox create review-auth --checkout feature/auth  # exact existing branch
treebox create fix-auth --repo /path/to/repo
treebox enter fix-auth                       # re-launch; refresh .env, re-sync deps if changed
treebox enter fix-auth -- "continue the refactor"  # args after -- go to the agent verbatim
treebox list
treebox teardown fix-auth --force
treebox teardown fix-auth --force --delete-branch
```

Provision without launching the agent (for scripting / over SSH / when you only need the worktree prepared):

```bash
treebox create fix-auth --dry-run  # print the git/setup commands it would run; change nothing
treebox create fix-auth --print    # prints the launch command (self-contained: carries the worktree dir, runnable from anywhere), no launch
treebox create fix-auth --json     # JSON {schemaVersion, name, worktree_path, branch, base, entry_command, created}
treebox list --json                     # JSON {schemaVersion, worktrees:[…]}
treebox doctor --json                   # JSON health report; exits non-zero if a hard check fails
```

## For agents: exit codes & errors

Branch on the **exit code**, not the text:

| code | meaning |
|------|---------|
| `0` | success |
| `1` | runtime failure, missing runner dependency, or failed doctor hard check |
| `2` | usage — bad name/branch/config/ref or option |
| `3` | not-found — the worktree the command needs doesn't exist |
| `4` | auth/permission — `origin` fetch failed |
| `5` | conflict — already exists / worktree lock held / uncommitted changes |

With `--json`, a failure prints **one object on stderr**: `{"schemaVersion":1,"error":{"code":"NOT_FOUND","message":"…","hint":"…"}}`; `path` may also appear for path-specific failures such as `DIRTY_WORKTREE`. Read `error.code`, surface `error.hint`. Every JSON payload carries `schemaVersion` and only gains fields within a version (don't reject extra keys).

## Usage tips

- **Default base is `main`** (configurable). A new branch created without `--base` is cut from `origin/main`; pass `--base <trunk>` when the repo's trunk is `master`, `dev`, etc.
- Ask before using `--delete-branch` or `--remove-volumes`. Defaults keep both.
- `create NAME` uses NAME as the branch, created fresh from `origin/<base>` — slug tokens joined by slashes are fine (`feature/auth` lives in dir `feature--auth`). A branch that already exists is a `BRANCH_EXISTS` conflict (exit `5`): resume it with `create --checkout <branch>` instead, which likewise flattens the directory name unless you pass `NAME` explicitly (default `<root>` = `.treebox/worktrees` under the repo; override with `--root`).
- Every worktree gets a pre-push guard that blocks pushing any `treebox/*` ref: generated placeholder branches must be renamed (`git branch -m <type>/<short-name>`) before push; explicitly named branches push as-is.
- `create` launches the agent (Claude by default) after provisioning and exits with the agent process's exit code. Use `--harness codex`, or `--print` / `--json` / `--dry-run` to avoid launching.
- Re-running `create` on a fully provisioned same-name worktree is a `SLUG_CONFLICT` (exit `5`), not an idempotent enter. Use `enter` to re-launch. If the dir exists but setup never finished (a prior run died mid-provision), `create` resumes setup instead of launching into a half-built tree.
- **Freshness is enforced.** `create` *requires* a successful `git fetch origin` and branches from the freshly-fetched `origin/<base>` (preferred over a stale local branch). If the fetch fails it exits `4` and does **not** fall back to stale refs. In a terminal, git/ssh will prompt for credentials (no pre-loaded ssh-agent needed); headless/`--json` runs need working non-interactive auth. Pass `--no-fetch` only when the user explicitly accepts possibly-stale local refs (e.g. offline). A repo with no `origin` skips the fetch. Run `doctor` to check origin reachability up front.
- `enter` recomputes the dependency-manifest hash and re-syncs only when deps changed; it always refreshes `.env`. It reuses the worktree's recorded isolation/firewall/harness/template defaults, so config changes do not silently change an existing worktree. A mismatched explicit `--isolation` is a conflict; explicit `--harness` and `--template` are per-session overrides. Pass extra agent args after `--`. `--cold` bypasses the shared cache.
- `--cold` is the escape hatch for a corrupted cache: from-source dependency resolution.
- `teardown` confirms interactively. Without a TTY (scripts/agents) or with `--json` it refuses unless `--force` (exit `5`); `--force` is also required to remove a worktree with uncommitted changes. `--json` prints a record of what was removed to stdout. `--skip-container` leaves containers/images alone; cleanup failures are best-effort (`container: "failed"`) and do not undo worktree removal. With host isolation there are no containers/volumes to remove.
- Only one `create`/`enter`/`teardown` may operate on a given worktree name at a time (a per-name lock, exit `5` if held). Wait rather than retrying immediately.
- Output: a compact spinner + green checkmarks with right-aligned timings on stderr, degrading gracefully off-TTY and under `NO_COLOR` (no color/animation). `--verbose` streams raw command output; `--quiet` suppresses progress. `--json` success payloads and `--print` commands write to stdout; human `--dry-run` writes its plan to stderr, while `--dry-run --json` writes JSON to stdout.
- The container firewall is off by default; pass `--firewall` to merge in the generated outbound allowlist. `--firewall`/`--no-firewall` is a tri-state override — an explicit flag wins in either direction and absence falls through to the `firewall` config default (so `--no-firewall` opts a single run out of a `firewall = true` config). The created-time choice is recorded, so `enter` reuses it rather than re-resolving the config.

## Notes

- Authentication is **subscription-login only**: a one-time `claude` / `codex` login on the host (`~/.claude`, `~/.codex`) is reused everywhere. `doctor` verifies those dirs. `ANTHROPIC_API_KEY` is never used.
- Defaults (`isolation`, `harness`, `base`, `root`, `env_file`, `firewall`, caches, custom setup hook) are configurable via a user-level TOML at `$TREEBOX_CONFIG`, else `$TREEBOX_HOME/config.toml` (default `~/.treebox/config.toml`). There is no repo-level config (a repo you don't trust must not run host commands). Path-valued settings (`root`, `env_file`, `caches` entries, the `TREEBOX_*` env vars) and the `--repo`/`--root` flags expand a leading `~`; plain relative `root`/`env_file` values stay repo-relative.
- The sandbox container is always defined by the operator template, never by anything in the target repo. Templates are named (`--template <name>`, default `default`): resolved from `$TREEBOX_TEMPLATE_DIR`, then `$TREEBOX_HOME/templates/<name>/` (default `~/.treebox/templates/<name>/`), then the bundled default (only for `default`). An unknown name is a loud error, not a fallback. The rendered config + build context live *outside* the mounted worktree (`<root>/.containers/<branch>/`) and are never mounted in, so the boxed agent can't read or edit its own cage.
- Shared package caches live on the host and are reused across worktrees (and bind-mounted into containers), so a fresh worktree sets up at near warm-tree speed.
