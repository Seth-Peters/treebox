# Usage

The core worktree lifecycle is five commands:

```bash
treebox doctor                          # 0 · is this host ready?
treebox create                          # 1 · provision + launch (name generated)
treebox enter brave-otter --harness codex  # 2 · come back later
treebox list                            # 3 · what exists, what's stale
treebox teardown brave-otter            # 4 · clean up (variadic: name several)
```

Two commands sit alongside the lifecycle: [`template`](#template-scaffold-and-inspect-sandbox-templates)
scaffolds and inspects the docker sandbox templates, and `version` prints the
installed version.

Worktree commands run against the current repo; pass `--repo PATH` to point
elsewhere, or `--root DIR` to override the worktree root for this invocation.
`treebox version`, `treebox --version`, and `treebox -V` print the installed
version.

## `create` — provision and launch

```bash
treebox create                  # generated name (brave-otter), placeholder branch
treebox create fix-auth         # named up front: works on branch fix-auth
treebox create feature/auth     # slash names: branch feature/auth, dir feature--auth
treebox create --checkout feature/auth  # exact existing branch (resume work, PR review)
```

Fetches `origin`, cuts a worktree from the fresh `origin/main`, copies your
`.env` and submodules, syncs dependencies from the shared cache, records the
lockfile hash, and launches the agent.

An explicit `NAME` is intentional, so it **is the branch**, created fresh
from `origin/<base>`: slug tokens joined by slashes
(`fix-auth`, `feature/user-auth`), with the slashes flattened to `--` in the
directory. The flattened form is the worktree's **permanent identity**: the
directory (`.treebox/worktrees/<name>`), the lock key, and what `enter` /
`teardown` address. The `treebox/` prefix is reserved for generated
placeholders and rejected as a name.

You never *have* to invent a branch name up front, though. Omit `NAME` and
treebox generates a petname, and the worktree starts on a **placeholder
branch** `treebox/<petname>`. Placeholders are **un-pushable by design**:
every worktree carries a per-worktree pre-push guard that rejects any
`treebox/*` ref with instructions to name the work first —

```text
✗ treebox: refusing to push placeholder branch 'treebox/brave-otter'.
  ↳ name this work first, then push again:  git branch -m <type>/<short-name>
  ↳ we use conventional-commits style branch names — pick the type that fits
    the change: feature/user-auth, fix/login-race, chore/bump-deps,
    docs/api-guide, refactor/db-layer, test/flaky-suite, …
  ↳ the treebox/ prefix marks auto-generated names; PRs should never carry them.
```

The guard's suggestion mirrors conventional-commit types, so the branch name an
agent picks (`feature/user-auth`, `fix/login-race`, `chore/bump-deps`, …)
describes the change the same way its commit subjects do.

The branch is a *mutable attribute*: rename it whenever the work takes shape
(`git branch -m fix/flaky-teardown`) — the directory never moves, and `list` /
`enter` / `teardown` follow the rename automatically. `git push --no-verify`
remains the deliberate human escape hatch.

`--checkout` checks out an **existing** branch (local or `origin/`) exactly —
for resuming work or reviewing a PR. The worktree's name derives from the
branch (`feature/auth` → `feature--auth`) unless you pass `NAME` too. It is
also the deliberate way to resume a branch you own: `create NAME` for a branch
that already exists (locally or on origin) is a loud `BRANCH_EXISTS` conflict,
never a silent adoption of old work.

Sandbox any of these by picking docker isolation:

```bash
treebox create fix-auth --isolation docker
```

| Option           | Effect                                                                    |
| ---------------- | ------------------------------------------------------------------------- |
| `--checkout BRANCH` | Check out this exact existing branch instead of creating a new one.    |
| `--base BRANCH`  | Base for the new branch — any branch, not just `main`. Resolved as `origin/<base>`. |
| `--isolation MODE` | `host` (default) or `docker`.                                           |
| `--harness, -H NAME` | Agent harness to launch: `claude` or `codex`.                        |
| `--cold`         | Bypass shared caches for a from-source build.                             |
| `--no-fetch`     | Opt out of the required `origin` fetch and accept possibly stale refs.    |
| `--firewall/--no-firewall` | Enable/disable the container firewall (docker isolation). Unset: the config default applies. |
| `--template NAME`| Operator-owned sandbox template to render.                                |
| `--dry-run, -n`  | Print the exact commands that would run; change nothing.                  |
| `--print`        | Provision, then print the launch command instead of launching.            |
| `--json`         | Provision, then print a JSON result instead of launching.                 |

A name that already exists is a loud conflict (exit `5`,
`error.code: "SLUG_CONFLICT"`) with the ways out — `enter` it, `teardown` it,
or pick another name. It is never silently reused. If a previous `create` died
after registering the worktree but before setup completed, running the same
`create` again finishes setup instead; a fully provisioned same-name worktree
is still a conflict.

### Stacking on another branch

`--base` isn't limited to `main`. Point it at `dev`, a release branch, or an
existing PR's branch to build on top of it in a fresh worktree:

```bash
treebox create auth-fixes --base feature/auth
```

This works even while `feature/auth` is checked out in another worktree — the
new worktree checks out its own *new* branch, so git's
one-checkout-per-branch rule never triggers. The base resolves as the freshly
fetched `origin/feature/auth` (falling back to the local branch only if it was
never pushed), so push the base first if its latest commits only exist locally.

Not sure what it will do? Ask first — `--dry-run` (`-n`) prints the exact
`git` / setup commands:

```console
$ treebox create fix-auth --dry-run

  ● create   fix-auth  ·  dry run

    worktree        .treebox/worktrees/fix-auth
    branch          fix-auth
    isolation       host  →  claude

    $ git -C ~/code/myapp fetch origin --quiet
    $ git -C ~/code/myapp worktree add -b fix-auth ~/code/myapp/.treebox/worktrees/fix-auth origin/main
    # install pre-push guard: per-worktree core.hooksPath -> <private git dir>/treebox-hooks (treebox/* refs are un-pushable)
    $ cp .env ~/code/myapp/.treebox/worktrees/fix-auth/.env
    $ uv sync
```

## `enter` — come back to a worktree

```bash
treebox enter fix-auth --harness claude
treebox enter fix-auth --harness codex -- --resume
```

`enter` (and `teardown`) take a **ref**: the worktree name, its *current*
branch, or a unique substring of either — resolved live from git, so a branch
the agent renamed five minutes ago still works. An ambiguous ref is a loud
exit `2` listing the matches, never a guess. If the ref names a branch that
exists but has no worktree (never materialized, or torn down), the `NOT_FOUND`
error hints at `treebox create --checkout REF` instead of the generic advice.

Re-enters the worktree and launches the agent. By default it reuses the
harness the worktree was **provisioned with** (`create -H codex` then plain
`enter` launches codex, not the config default); an explicit `--harness` is a
per-session override that launches that agent this time without changing what's
recorded on disk. The sandbox **template** is reused the same way: a worktree
created with `--template node` re-renders that template on `enter`, not the
config default, and an explicit `--template` is a per-session override. The
recorded **isolation** mode also wins over the config default; a conflicting
explicit `--isolation` exits `5` instead of entering the wrong kind of
worktree. For docker worktrees, the recorded firewall choice is reused too, so
`create --no-firewall` keeps entering cleanly even under a `firewall = true`
config.
Dependencies re-sync **only if the lockfile changed** since the last setup
(treebox stores the hash in the worktree's private git dir, so it never shows
up in `git status`); a re-sync preserves the recorded harness and template
rather than stamping in the session's choice. Anything after `--` is passed
through to the agent. `--cold` forces a cache-bypassing re-sync.

Under docker isolation, `enter` **preflights the Docker daemon first** (like
`create` and `doctor`), so a stopped daemon fails fast with a clean
`DOCKER_UNAVAILABLE` error and a start-docker hint rather than a misleading
error deeper in launch. It also **re-stages the scoped credential copies**
from the host on every entry — independent of the lockfile-hash skip — so a
host logout/revocation drops the stale copies and a fresh login reaches the
sandbox on the very next entry.

## `list` — what exists, what's stale

(`treebox ls` works too.)

```console
$ treebox list

  NAME           BRANCH                 LAST COMMIT              AGE    DEPS       ENV
  ─────────────────────────────────────────────────────────────────────────────────────────
  fix-auth       fix/ci-caching         cache uv wheels in CI     2h    ● fresh    ● present
  brave-otter    ⚠ unnamed              initial provision         3d    ● stale    ● present

  2 worktrees · 1 unnamed · 1 stale
  ↳ unnamed: rename before push — git branch -m <type>/<short-name>
```

Sorted by recency — the worktree you want is almost always the one that just
committed. The **name** is the stable handle; the **branch** is read live.
A placeholder branch is always `treebox/<name>`, so its row just shows
`⚠ unnamed` — work that can't be pushed yet is visible at a glance. `stale`
means the lockfile changed since the last dependency sync — the next `enter`
fixes that automatically.

## `teardown` — clean up

(`treebox rm` works too.)

```bash
treebox teardown                                # pick interactively (no refs)
treebox teardown fix-auth                       # remove one worktree
treebox teardown fix-auth brave-otter --force   # several at once
treebox teardown fix-auth --delete-branch       # ...and its local branch
```

Takes one or more refs (name, branch, or unique substring). Every ref is
resolved before anything is removed — a typo among three targets removes
nothing. Refuses to remove a worktree with uncommitted changes unless you pass
`--force`. For docker-sandboxed worktrees, `--remove-volumes` also removes
treebox volumes and `--skip-container` leaves containers/images untouched.
`--json` prints a structured record of what was removed (and never blocks on
a prompt).

Container/image cleanup and local branch deletion are best-effort after the
target set is chosen: if a container or image removal fails, treebox warns,
still attempts every remaining cleanup step (volumes, sandbox files), removes
the worktree, and reports `container: "failed"`; a failed volume removal
alone warns and reports `volumes_removed: false` without marking the
container `failed`; if Docker itself is unavailable, the worktree is still
removed and the record reports `container: "skipped"` with
`volumes_removed: false`, even under `--remove-volumes`; if branch deletion
fails, it warns and reports `branch_deleted: false` without undoing the
worktree removal.

**Run it with no refs** and treebox walks you through the whole decision — an
arrow-key picker (`↑↓` to move, space to toggle, enter to confirm) over your
worktrees, each annotated with a "will I lose work?" badge; a second, smaller
picker for which of those also lose their local branch; then the removal:

```console
$ treebox teardown

? Select worktrees to tear down

 » ◉  calm-finch    feat/api      3d   ✓ merged (PR #42)
   ◯  brave-otter   fix/login     1d   ⇡ ahead 2 · unmerged
   ◉  trusty-robin  ⚠ unnamed     2h   ✎ uncommitted · never pushed

? Also delete the local branch for … (space to pick, enter to skip)

 » ◉  calm-finch    feat/api      3d
   ◯  trusty-robin  ⚠ unnamed     2h

  ● teardown   calm-finch

  · container       n/a (host isolation)
  ✓ worktree        removed .treebox/worktrees/calm-finch
  ✓ branch          deleted feat/api

  ✗ trusty-robin    kept · uncommitted changes
    ↳ commit or stash the changes, or re-run teardown with --force

  Removed 1 worktree · kept 1 with uncommitted changes.
```

The badge is pure local git — dirty tree, ahead/behind, merged into your base,
never-pushed — so it works on **any** remote (GitHub, GitLab, Bitbucket, plain
git) and any auth. A freshly created worktree with no commits of its own reads
`⚠ empty` (safe to delete, but not to be mistaken for a landed branch) rather
than `✓ merged`, since a placeholder sitting exactly at your base trivially
counts as "merged into" it. If a forge CLI you're already logged into is present
(`gh` for GitHub, `glab` for GitLab), the picker also shows the branch's PR/MR
state — which is the only reliable way to spot a **squash** merge. Everything
degrades cleanly: no CLI, an unknown host, or offline just falls back to the
local badge.

The branch question is per-worktree, not one switch for the batch — drop a
merged branch and keep an in-progress one in the same pass, so
`--delete-branch` isn't something you have to remember up front. `Ctrl+C` at
either question backs out of the whole run with nothing removed.

Picking in the chooser stands in for the confirm prompt, and a **mixed
selection just works**: the clean worktrees are torn down while any with
uncommitted changes are **kept and reported** (like `trusty-robin` above), so
one dirty tree never blocks the rest. treebox exits `5` when it kept anything,
so a script can still tell the run wasn't total. (Passing refs explicitly stays
all-or-nothing — naming a dirty tree stops the whole run. `--force` removes
dirty worktrees either way.) The chooser is interactive-only: under `--json`
or a non-TTY, pass refs explicitly.

If a worktree's recorded isolation mode is unknown (corrupt or hand-edited
state), teardown refuses it as a conflict rather than guessing how to drive its
container — the same stance `enter` takes. Pass `--skip-container` to remove the
tree and its branch anyway and clear any leftover container yourself.

## `doctor` — is this host ready?

```bash
treebox doctor                       # checks for host isolation
treebox doctor --isolation docker    # also checks the Docker daemon
```

Checks git, agent logins, `.env`, and — because `create` requires a fresh
fetch — whether git can authenticate to `origin` without a prompt. A missing
`.env` is not a failure: the row renders as a muted `·` note marked
"optional" (the configured path still shown), since `create` simply skips the
copy. Every failing row comes with the command that fixes it. Hard failures
(`git`, `repo`, or the selected isolation mode) exit `1` in both human and
`--json` modes, so `treebox doctor --json && treebox create ...` works in
scripts.

## `template` — scaffold and inspect sandbox templates

Docker isolation renders an **operator-owned template** — a `Dockerfile` +
`container.json` (and the firewall scripts) — into a host-side dir *beside* the
worktree. The `template` command is the sanctioned way to fork and inspect those
templates; it works from any install, so customizing a sandbox never means
reaching into the package internals. Named templates live under
`$TREEBOX_HOME/templates/<name>` (see [configuration](configuration.md#customizing-the-sandbox)).

```bash
treebox template init node                 # copy the built-in default → ~/.treebox/templates/node, then edit
treebox template init node --from python   # fork one of your own instead
treebox template list                      # names, source, required-file status, and the config default (ls works too)
treebox template path node                 # print the resolved dir: cd "$(treebox template path node)"
```

`template init <name>` always yields a directory with the **full required file
set**, so `create --template <name>` can't fail on a half-copied template; it
refuses to clobber an existing template without `--force`. Then edit the
`Dockerfile` and `container.json` and launch with
`treebox create <name> --isolation docker --template <name>`. `template list`
flags any template dir missing a required file before `create` does, and marks
which one is your config default. `template path [<name>]` (default: `default`)
prints the resolved location — the install-agnostic answer to "where does this
template live", including the bundled default. All three take `--json` for
scripting.

## Scripting against treebox

treebox is built to be driven by other programs (including agents). Data goes
to **stdout**, diagnostics to **stderr**, and exit codes are stable:

| Code | Meaning                                                |
| :--: | ------------------------------------------------------ |
| `0`  | ok                                                     |
| `1`  | runtime failure, missing runner dependency, or failed doctor hard check |
| `2`  | usage — invalid name/branch, ambiguous ref, bad option |
| `3`  | not found — the worktree/branch doesn't exist          |
| `4`  | auth — fetch or credential problem                     |
| `5`  | conflict — name taken, dirty tree, or lock held        |

The worktree commands and `doctor` take `--json`; payloads carry a
`schemaVersion` — fields are only ever added within a version, and a breaking
reshape or rename bumps it. Agents branch on these payloads, so treat the shape
as a contract:

```console
$ treebox create fix-auth --json
{
  "schemaVersion": 1,
  "name": "fix-auth",
  "worktree_path": "/home/you/code/myapp/.treebox/worktrees/fix-auth",
  "branch": "treebox/fix-auth",
  "base": "main",
  "entry_command": ["sh", "-c", "cd /home/you/code/myapp/.treebox/worktrees/fix-auth && exec claude --dangerously-skip-permissions"],
  "created": true
}
```

With `--json` (or `--print`) treebox provisions but does **not** launch the
agent — it hands you the launch command instead, so your script decides when
to run it. The command is self-contained for both isolation modes: it carries
the worktree directory (`cd … && exec …` on host, `docker exec -w …` in
docker), so replaying it from any directory launches the agent in the box.
Without `--json`, `--print`, or `--dry-run`, `create` and `enter` launch the
agent and exit with the agent process's exit code. Human `--dry-run` writes the
would-run plan to stderr; `--dry-run --json` writes its payload to stdout.

Current success payloads:

| Command | Top-level fields |
| ------- | ---------------- |
| `create --json`, `enter --json` | `schemaVersion`, `name`, `worktree_path`, `branch`, `base`, `entry_command`, `created` |
| `create --dry-run --json` | `schemaVersion`, `dry_run`, `name`, `worktree_path`, `branch`, `commands` |
| `list --json` | `schemaVersion`, `worktrees` |
| `teardown --json` | `schemaVersion`, `worktrees` |
| `doctor --json` | `schemaVersion`, `ok`, `isolation`, `checks`, `advisories` |

`list` rows contain `name`, `branch`, `unnamed`, `missing`, `last_commit`,
`commit_epoch`, `path`, `base`, `isolation`, `harness`, `deps`, and `env`.
`deps` is `fresh`, `stale`, or `unknown`; `env` is `present` or `absent`.
`teardown` records contain `name`, `branch`, `worktree_path`, `removed`,
`branch_deleted`, `container`, and `volumes_removed`; `container` is `cleaned`,
`skipped`, or `failed`. Each field reports what the runner actually did to
that resource: `container` is `skipped` when cleanup didn't run
(`--skip-container`, or Docker unavailable) and `failed` when a container or
image removal failed, while `volumes_removed` is `true` only when docker
volumes were really removed - never on host isolation, not merely because
`--remove-volumes` was passed, and a failed volume removal leaves it `false`
without marking `container` failed. `doctor` checks contain `name`,
`ok`, and `detail`.

JSON errors are emitted to stderr as:

```json
{
  "schemaVersion": 1,
  "error": {
    "code": "NOT_FOUND",
    "message": "...",
    "hint": "..."
  }
}
```

`error.code` and `error.message` are always present; `error.hint` and
`error.path` appear when treebox has useful remediation or a path-specific
failure.

`error.code` and the exit code are the scripting contract:

| `error.code` | Exit | Common trigger |
| ------------ | :--: | -------------- |
| `INVALID_CONFIG` | `2` | Bad config file or invalid `--isolation` / `--harness`. |
| `INVALID_NAME` | `2` | `create NAME` is not slash-separated lowercase slugs, or uses the reserved `treebox/` prefix. |
| `INVALID_BRANCH` | `2` | `create NAME` or `--checkout` names an invalid git ref. |
| `NOT_A_REPO` | `2` | `--repo` is not a git repo. |
| `AMBIGUOUS_REF` | `2` | A ref matches more than one worktree. |
| `NOT_FOUND` | `3` | The requested worktree, checkout branch, or base branch does not exist. |
| `FETCH_FAILED` | `4` | Required fetch/auth failed. |
| `MISSING_DEPENDENCY` | `1` | Required runner dependency is missing. |
| `DOCKER_UNAVAILABLE` | `1` | Docker is installed but the daemon is unavailable. |
| `ERROR` | `1` | Unclassified runtime, setup, or template failure. |
| `SLUG_CONFLICT` | `5` | The worktree name is already taken. |
| `BRANCH_EXISTS` | `5` | `create NAME` names a branch that already exists — resume it with `--checkout`. |
| `BRANCH_IN_USE` | `5` | The `--checkout` branch is already checked out in another worktree. |
| `DIRTY_WORKTREE` | `5` | Explicit teardown target has uncommitted changes. |
| `NEEDS_CONFIRMATION` | `5` | Teardown would need an interactive choice or confirmation. |
| `LOCK_HELD` | `5` | Another treebox operation holds this worktree's lock. |
| `UNKNOWN_ISOLATION` | `5` | Recorded isolation mode is unknown (corrupt or hand-edited state). |
| `ISOLATION_MISMATCH` | `5` | Explicit `--isolation` disagrees with the recorded mode. |
| `TEMPLATE_NOT_FOUND` | `3` | `template init --from` / `template path` names a template that doesn't exist. |
| `TEMPLATE_EXISTS` | `5` | `template init` names an existing template — pass `--force` to overwrite. |
| `TEMPLATE_CONFLICT` | `2` | `template init` source and destination are the same template. |
