---
name: treebox
description: Create, enter, list, and tear down isolated git worktree sandboxes (host-native or docker-backed) through the treebox CLI. Use when the user asks to create a worktree, spin up an agent sandbox, enter a sandbox, list sandbox worktrees, or tear one down.
---

# Treebox

Use the `treebox` CLI for all mechanics. Do not run `git worktree add`, `git worktree remove`, or Docker cleanup commands manually unless diagnosing a CLI failure.

A pluggable **isolation mode** decides where the agent runs:
- `--isolation host` (default): provisions the worktree and launches the agent directly on the host, in the worktree dir. No container, no Docker — the right default on an SSH box or a trusted machine.
- `--isolation docker`: same provisioning, but launches the agent inside a docker container with the shared caches and scoped copies of the `~/.claude`/`~/.codex` login files mounted in. Needs only `docker` and a non-root user: as root (uid 0) `create`/`enter`/`doctor` stop with `RUNNING_AS_ROOT` (exit `1`) before anything is provisioned. Fix: run treebox as a non-root user (`useradd --create-home --groups docker treebox`). Host isolation works as root.

Treebox itself requires Python 3.11 or newer. The bundled sandbox template is pinned to CPython 3.14.6.

Provisioning (fetch → resolve branch → worktree add → copy submodules → copy `.env` → cache-backed setup → record lockfile hash) is identical for both isolation modes; only the launch seam differs.

## Workflow

1. Run `treebox doctor` (add `--isolation docker` to check the Docker daemon when docker isolation will be used). It exits `1` when the selected agent cannot run: no harness binary on `PATH` (host) or no real login (`login: <harness>`; for docker the login file the sandbox gets a copy of).
2. If creating named work, ask for a short valid worktree name: lowercase slug tokens (`[a-z0-9-]+`) optionally joined by slashes (`fix-auth`, `feature/user-auth`). The name is used directly as the branch name (never the reserved `treebox/` prefix); slashes flatten to `--` in the directory. Omit it only when the user wants treebox to generate a petname on a guarded `treebox/` placeholder branch.
3. Create with `treebox create <name> --base <trunk>`, or use `treebox create [name] --checkout <existing-branch>` to resume/review an existing branch.
4. If any command exits non-zero, stop and show the full error (and its `hint`). Do not retry automatically — branch on the exit code (below).
5. Report that the CLI launched the agent, or show the error that prevented launch.

## Command reference

- `create [NAME] [-- AGENT_ARGS…]` - provision and launch; args after `--` go to the agent (they need a NAME), and with `--json`/`--print` they are part of `entry_command`. Flags: `--base <branch>` `--checkout <existing-branch>` `--isolation host|docker` `--harness claude|codex` `--template <name>` `--cold` `--no-fetch` `--firewall/--no-firewall` `--dry-run` `--print` `--json` `--repo <path>` `--root <dir>` `--quiet` `--verbose`.
- `enter REF [-- AGENT_ARGS…]` — REF is a name, branch, unique substring, or an absolute worktree path (exact: only the worktree registered there; scripts that retry use it, so a retry never hits another worktree). Re-launch in an existing worktree; refreshes `.env`, reuses the isolation/firewall/harness/template from the worktree's host-owned record (never from files the agent can write), and re-syncs deps only if the lockfile changed (an interrupted setup is finished rather than skipped). Flags: `--isolation` `--harness` `--template <name>` `--cold` `--print` `--json` `--repo` `--root` `--quiet` `--verbose`.
- `list` (alias `ls`) - table of worktrees (branch · recorded isolation · deps freshness · `.env` presence). For directory paths, read the [list output reference](https://seth-peters.github.io/treebox/usage/#list-what-exists-whats-stale). A worktree without a host-owned record is shown honestly as `unknown`. Flags: `--repo` `--root` `--json`.
- `teardown [REF...]` (alias `rm`) - remove selected worktrees; caches are left intact. Flags: `--yes`/`-y` (confirm without a prompt; uncommitted changes still block) `--force` `--delete-branch` `--remove-volumes` `--skip-container` `--json` `--repo` `--root` `--quiet` `--verbose`.
- `template init NAME` — scaffold an operator-owned sandbox template into `$TREEBOX_HOME/templates/<name>` (copies the full required file set from the default). Flags: `--from <template>` `--force` `--json`. Then edit its `Dockerfile`/`container.json` and select with `--template <name>`.
- `template list` (alias `ls`) — table of templates (name · source · firewall · status; status flags missing required files and malformed JSON, firewall shows whether the template can serve `--firewall`) with the configured default flagged; when `default` still resolves to the bundled image, also highlights what that image ships. Flags: `--json`.
- `template path [NAME]` — print a template's resolved directory (for scripting: `cd "$(treebox template path <name>)"`).
- `doctor` - check git, the selected agent (`agent: <harness>`) and its login (`login: <harness>`), UID/GID, and isolation deps. Flags: `--isolation` `--harness`/`-H` `--repo` `--json`.
- `version` / `--version` / `-V` — print the version.
- Global `--non-interactive` (before the command; or `TREEBOX_NON_INTERACTIVE=1`) - unattended mode, also with a terminal attached: no picker, prompt, or credential prompt; what would need an answer fails with a classified error.

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
treebox teardown fix-auth --yes             # confirm without a prompt; refuses uncommitted work
treebox teardown fix-auth --force           # also remove uncommitted work
treebox teardown fix-auth --force --delete-branch
```

Provision without launching the agent (for scripting / over SSH / when you only need the worktree prepared):

```bash
treebox create fix-auth --dry-run  # print a stable side-effect-free plan of the git/setup commands; no launch
treebox create fix-auth --print    # prints the launch command (self-contained: carries the worktree dir, runnable from anywhere), no launch
treebox create fix-auth --json     # JSON {schemaVersion, name, worktree_path, branch, base, entry_command, created, warnings}
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
| `3` | not-found — the worktree, branch, or template the command needs doesn't exist |
| `4` | auth/permission — `origin` fetch failed |
| `5` | conflict — already exists / worktree lock held / uncommitted changes |

With `--json`, a failure prints **one object on stderr**: `{"schemaVersion":1,"error":{"code":"NOT_FOUND","message":"…","hint":"…"}}`; `path` may also appear for path-specific failures such as `DIRTY_WORKTREE`. Read `error.code`, surface `error.hint`. Every JSON payload carries `schemaVersion` and only gains fields within a version (don't reject extra keys).

Two failures you can fix and retry with the **same command**:

- **`SETUP_FAILED` (exit `1`) - a failed setup is a failed `create` / `enter`.** The message names the step and its exit status; `error.path` is the file with the step's output. Read it, fix the cause (a broken lockfile, a missing tool), then run `treebox enter <name>` or the same `create` again: the worktree is kept and its setup is retried. Never treat the worktree as ready until setup succeeds.
- **`TEARDOWN_INCOMPLETE` (exit `1`) - an incomplete cleanup is a failed `teardown`.** The message (and `remaining` in the `--json` record) names what is still present, for example a container `docker rm` could not remove. The worktree and its ownership record are kept; fix the cause (start Docker, free the resource) and run the same `teardown` again.

## Unattended runs (no terminal input)

When you drive treebox for another agent or a job, use unattended mode so nothing can wait on a terminal, even when one is attached: `export TREEBOX_NON_INTERACTIVE=1` (or `treebox --non-interactive <command>`). The documented clean flow:

```bash
treebox doctor --json > /dev/null              # exit 1: the agent cannot run here
out=$(treebox create fix-login --json -- -p "Fix the login race and commit")
eval "$(jq -r '.entry_command | @sh' <<<"$out")"   # runs the agent; $? is its exit code
treebox list --json | jq '.worktrees[] | select(.name == "fix-login")'
treebox teardown fix-login --yes --json        # exit 5 DIRTY_WORKTREE if work is uncommitted
```

- Check `warnings` in each success payload (for example a skipped `.env` copy).
- A missing prerequisite is a classified failure, never a prompt: `FETCH_FAILED` (a fetch that needs credentials), `HARNESS_MISSING` / `LOGIN_MISSING` (exit `1`, before anything is provisioned), `NEEDS_CONFIRMATION` (teardown without `--yes`).
- `treebox create NAME -- ARGS` launches the agent itself; then a signal to treebox stops the agent (also inside a docker container) and exits `128 + signal` with `CANCELLED`. Stopping a replayed docker `entry_command` does not stop the agent in the container.

## Usage tips

- **Default base is `main`** (configurable). A new branch created without `--base` is cut from `origin/main`; pass `--base <trunk>` when the repo's trunk is `master`, `dev`, etc.
- Ask before using `--delete-branch` or `--remove-volumes`. Defaults keep both.
- `create NAME` uses NAME as the branch, created fresh from `origin/<base>` — slug tokens joined by slashes are fine (`feature/auth` lives in dir `feature--auth`). A branch that already exists is a `BRANCH_EXISTS` conflict (exit `5`): resume it with `create --checkout <branch>` instead, which likewise flattens the directory name unless you pass `NAME` explicitly (default `<root>` = `.treebox/worktrees` under the repo; override with `--root`).
- Every worktree gets a pre-push guard that blocks pushing any `treebox/*` ref: generated placeholder branches must be renamed (`git branch -m <type>/<short-name>`) before push; explicitly named branches push as-is.
- `create` launches the agent (Claude by default) after provisioning and exits with the agent process's exit code. Use `--harness codex`, or `--print` / `--json` / `--dry-run` to avoid launching. When the agent exits non-zero, the last stderr line is `treebox: agent exited with status N`; a treebox failure never prints that line (it prints its classified error instead), so that line is how you tell an agent exit of `1`-`5` from a treebox failure. A docker sandbox that stops or restarts while the agent runs is a treebox failure (exit `1`, no agent line): the agent's result is unknown, so `enter` again. SIGINT / SIGTERM / SIGHUP to treebox stop the agent (inside the container for docker; the container keeps running) and exit `130` / `143` / `129` with `CANCELLED`.
- A `SLUG_CONFLICT` whose hint says the directory is **not a registered worktree** is a stray directory git never knew about: `treebox teardown` cannot see it (it would exit `3`), so follow the hint's `rm -rf <path>` and re-run `create`. A registered-but-corrupt worktree is the other case, and its hint points at `treebox teardown <name>`.
- Re-running `create` on a fully provisioned same-name worktree is a `SLUG_CONFLICT` (exit `5`), not an idempotent enter. Use `enter` to re-launch. If the dir exists but setup did not finish (a prior run was interrupted or a setup step failed), `create` resumes setup instead of launching into a half-built tree.
- `list --json` rows carry `health` (`healthy`, `directory_missing`, `invalid_linkage`, `unregistered`, `unreadable_state`) and `setup` (`pending`, `failed`, `done`, or `""` without a record). `deps` is `fresh`/`stale` only after setup is `done`. The older `missing` field is `true` exactly when `health` is `directory_missing` or `invalid_linkage`. `enter` on an `invalid_linkage` worktree is `INVALID_LINKAGE` (exit `5`); recover it with `treebox teardown <name>`.
- Before using `--dry-run` as a preflight, read the [dry-run checks and limits](https://seth-peters.github.io/treebox/usage/#dry-run-checks).
- **`.env` is skipped, not forced, over a directory.** If the worktree has a directory at `.env` (the branch commits `.env/`, or an agent made one), `create`/`enter` still succeed: treebox leaves the directory untouched, warns on stderr (`.env not copied`), and `list --json` shows `"env": "absent"`. Under `--json` the warning is in the payload's `warnings` list (always present, `[]` when nothing was skipped). `.gitmodules` follows the same rule.
- **Freshness is enforced.** `create` *requires* a successful `git fetch origin` and branches from the freshly-fetched `origin/<base>` (preferred over a stale local branch). If the fetch fails it exits `4` and does **not** fall back to stale refs. In a terminal, git/ssh will prompt for credentials (no pre-loaded ssh-agent needed); headless runs and `--non-interactive` need working non-interactive auth (a fetch that would prompt fails at once with `FETCH_FAILED`, exit `4`). Pass `--no-fetch` only when the user explicitly accepts possibly-stale local refs (e.g. offline). A repo with no `origin` skips the fetch. Run `doctor` to check origin reachability up front.
- `enter` recomputes the dependency-manifest hash and re-syncs only when deps changed; if a prior setup was interrupted or failed, it runs setup again even when the hash is unchanged. It always refreshes `.env`. It reuses the worktree's recorded isolation/firewall/harness/template defaults, so config changes do not silently change an existing worktree. A mismatched explicit `--isolation` is a conflict; explicit `--harness` and `--template` are per-session overrides. Pass extra agent args after `--`. `--cold` bypasses the shared cache. For a docker worktree whose container stopped (host reboot, manual `docker stop`), the container is restarted and the firewall re-applied before the agent launches - and before a `--print`/`--json` command is emitted, so the emitted command is never dead on replay.
- `--cold` is the escape hatch for a corrupted cache: from-source dependency resolution.
- `teardown` confirms interactively. Without a TTY (scripts/agents), with `--json`, or with `--non-interactive` it refuses with `NEEDS_CONFIRMATION` (exit `5`) unless you pass `--yes` (confirms only: a worktree with uncommitted changes is still `DIRTY_WORKTREE`, exit `5`) or `--force` (also removes uncommitted changes; ask the user first). Uncommitted changes and git locks are checked again right before removal, so work written while the prompt is open is never deleted; a worktree locked with `git worktree lock` is never removed (`WORKTREE_LOCKED`, exit `5`, even with `--force`). A corrupt worktree (`health: invalid_linkage`, its `.git` pointer file missing) is never misreported as `DIRTY_WORKTREE` from the main checkout's uncommitted changes: it takes the normal confirmation path (`NEEDS_CONFIRMATION` under `--json`/non-TTY; `--yes` or `--force` removes the directory and git's stale registration without touching the main checkout; container cleanup still follows the isolation in the host-owned record, which does not depend on the missing pointer). A worktree that `enter` refuses with `RECORD_MISSING` or `RECORD_INVALID` (exit `5`; for example one made by another treebox version) is recreated: `teardown` it (no extra flag needed), then `create` it again. `--json` prints a record of what was removed to stdout, with `complete` and `remaining`. `--skip-container` leaves containers/images alone. Any cleanup step that does not finish (a container/image/volume removal fails, Docker is unavailable, git refuses to remove the tree) makes teardown fail with `TEARDOWN_INCOMPLETE` (exit `1`) and keeps the worktree and its record so the same command retries; a failed container/image removal is also `container: "failed"`, and a failed volume removal alone reports `volumes_removed: false` without marking the container `failed`. `volumes_removed` is `true` only when docker volumes were actually removed. With host isolation there are no containers/volumes to remove (`volumes_removed` is always `false`).
- For worktree lock conflicts, read the [locking rules](https://seth-peters.github.io/treebox/how-it-works/#built-to-be-scripted). Wait before retrying.
- **Remote workers.** To run tasks on another machine, run the whole treebox lifecycle on that worker over SSH; address each task by its `worktree_path` from `create --json`, never by name. Read the [remote workers contract](https://seth-peters.github.io/treebox/remote-workers/).
- **Dispatching several tasks at once is supported.** `create` (and `teardown`) calls for *different* names can be started in parallel against the same repo: they queue briefly on a repository lock for the git steps and then set up concurrently, so there is nothing to serialize yourself. A `create` that fails never leaves a branch behind - re-run the same command.
- Output: a compact spinner + green checkmarks with right-aligned timings on stderr, degrading gracefully off-TTY and under `NO_COLOR` (no color/animation). `--verbose` streams raw command output; `--quiet` suppresses progress. `--json` success payloads and `--print` commands write to stdout; human `--dry-run` writes its plan to stderr, while `--dry-run --json` writes JSON to stdout.
- The container firewall is off by default; pass `--firewall` to merge in the generated outbound allowlist. `--firewall`/`--no-firewall` is a tri-state override — an explicit flag wins in either direction and absence falls through to the `firewall` config default (so `--no-firewall` opts a single run out of a `firewall = true` config). The created-time choice is recorded, so `enter` reuses it rather than re-resolving the config.

## Notes

- Authentication is **subscription-login only**: a one-time `claude` / `codex` login on the host (`~/.claude`, `~/.codex`) is reused everywhere. `doctor` verifies a real login for the selected harness (the login file, or on macOS Claude Code's Keychain item for host isolation; docker needs the login file itself). `ANTHROPIC_API_KEY` is never used.
- Defaults (`isolation`, `harness`, `base`, `root`, `env_file`, `firewall`, caches, custom setup hook) are configurable via a user-level TOML at `$TREEBOX_CONFIG`, else `$TREEBOX_HOME/config.toml` (default `~/.treebox/config.toml`). There is no repo-level config (a repo you don't trust must not run host commands). Path-valued settings (`root`, `env_file`, `caches` entries, the `TREEBOX_*` env vars) and the `--repo`/`--root` flags expand a leading `~`; plain relative `root`/`env_file` values stay repo-relative.
- The sandbox container is always defined by the operator template, never by anything in the target repo. Templates are named (`--template <name>`, default `default`): resolved from `$TREEBOX_TEMPLATE_DIR`, then `$TREEBOX_HOME/templates/<name>/` (default `~/.treebox/templates/<name>/`), then the bundled default (only for `default`). An unknown name is a loud error, not a fallback. A name is an identifier (letters, digits, `.`, `_`, `-`), never a path: an absolute path or `..` exits `2`; point `$TREEBOX_TEMPLATE_DIR` at a directory instead. The rendered config + build context live *outside* the mounted worktree and are never mounted in, so the boxed agent can't read or edit its own cage. For their location, read the [resource ownership reference](https://seth-peters.github.io/treebox/how-it-works/#the-host-owned-record).
- Shared package caches live on the host and are reused across worktrees (and bind-mounted into containers), so a fresh worktree sets up at near warm-tree speed.
