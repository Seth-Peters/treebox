# How it works

treebox is orchestration glue — it shells out to `git` and `docker` rather
than reimplementing them. The whole tool is organized around one seam:
**provisioning always happens on the host; running is pluggable.**

Three closed vocabularies sit on top of that seam:

- **Harness** — which agent CLI launches (`claude`, `codex`).
- **Runner / isolation** — where it executes (`host`, `docker`).
- **Ecosystem** — what setup runs (uv, npm, pnpm, go, cargo).

## The provisioning pipeline

Every `create` walks the same host-side pipeline, regardless of isolation mode:

```text
fetch origin                 # required — a failure exits 4, loudly
  └─ resolve origin/<base>
       └─ git worktree add   # .treebox/worktrees/<name>, on <name> (or treebox/<petname>)
            └─ install pre-push guard     # treebox/* refs can't be pushed
                 └─ copy submodules
                      └─ copy .env
                           └─ runner.setup      # deps, from the shared cache
                                └─ record lockfile hash
                                     └─ hand off to the runner
```

The runner — picked with `--isolation` — only decides **where** the last two
steps happen:

| `--isolation` | `setup` runs…              | Agent runs…                             |
| -------- | ------------------------------- | --------------------------------------- |
| `host`   | in the worktree shell           | in the worktree shell                   |
| `docker` | inside the container, on create | via `docker exec`, inside the sandbox   |

Docker isolation builds and starts the sandbox with plain `docker build` /
`docker run`. The worktree and the repo's git dir are bind-mounted at their
host paths — mirrored 1:1 — so in-container `git` resolves the worktree's
pointers exactly as the host does, with no extra tooling.

## Names are identity; branches are mutable

The worktree **name** (the directory leaf) is the permanent identity: the
docker sandbox bind-mounts the absolute path and a live agent's CWD sits in
it, so the directory is never renamed. The **branch** is just an attribute.
An explicit `create NAME` starts on the branch `NAME` itself (slashes kept in
the branch, flattened to `--` in the directory); a nameless `create` starts
on a `treebox/<petname>` placeholder expected to be renamed (`git branch -m`)
once the work has a shape. `list`, `enter`, and `teardown` read branches live
from `git worktree list --porcelain`, so renames are followed automatically.

Placeholders are **un-pushable by design**. Every `create` installs a
pre-push hook scoped to the worktree — `extensions.worktreeConfig` plus a
per-worktree `core.hooksPath` pointing into the private git dir
(`.git/worktrees/<id>/treebox-hooks`), never the shared repo hooks. The hook
rejects any `treebox/*` ref with instructions to rename first, so an
auto-generated name can never become a PR title — from any worktree, whatever
branch it started on. Because the private git dir is bind-mounted 1:1 into
the sandbox, the guard binds in-container pushes too. It is a forcing
function, not a security boundary: `git push --no-verify` is the deliberate
human escape hatch.

## Freshness is enforced, not hoped for

`create` requires a successful `git fetch origin` and branches from the fresh
`origin/<base>`. A failed fetch exits `4` rather than silently building on
stale refs. `--no-fetch` is the only escape, and it's explicit.

After that, staleness is *tracked*: treebox hashes the ecosystem's manifest
files (lockfiles) at setup time and stores the hash in the worktree's private
git dir (`.git/worktrees/<id>/`). That state never appears in `git status`,
is pruned together with the worktree, and is what lets `enter` re-sync
dependencies only when they actually changed — and `list` show `fresh` /
`stale` at a glance.

The same private state records the worktree's creation-time choices. For an
existing worktree, `enter` and `teardown` recover the recorded isolation and
template instead of drifting to today's config defaults; `enter` also reuses the
recorded firewall, and it reuses the recorded harness unless a per-session
harness override is passed.
`teardown` reads that record through the repo's own worktree registration
rather than the worktree's `.git` pointer, so the recorded choices survive
even a corrupt worktree whose pointer file is gone.
An explicit `--isolation` that disagrees with the recorded mode is a conflict,
not an override.

## Warmth lives in the cache, not the tree

treebox detects the package manager from the repo — **uv, npm, pnpm, go, or
cargo** — and drives its cache-backed install. Installs hardlink out of shared
host caches (`~/.cache/uv`, the pnpm store, …) that are reused across
worktrees *and* mounted into containers. A tenth worktree costs seconds, not a
re-download. `--cold` bypasses the caches when you want a from-source build.
In host isolation, setup is a best-effort warm start: missing tools or failed
auto/custom setup steps warn and the agent still launches. Docker isolation's
`postCreate` remains part of sandbox setup and fails loudly.

## The sandbox config lives outside the box

Docker isolation's threat model is simple: **an agent must not be able to
edit the config that defines its own sandbox.**

- The container template (Dockerfile, `container.json`, firewall setup) is
  **operator-owned** — bundled with treebox or copied to your
  `~/.treebox/templates/`, never read from the target repo.
- It is rendered into a directory *beside* the worktree, outside the container
  mount. Inside the box, the agent simply cannot reach it.
- The target repo's own container config, treebox config, and setup hooks are
  **ignored** for the same reason — a repo-level config could run arbitrary
  commands on your host. treebox reads configuration from your user config
  only.

When treebox itself shells out to host-side `git`, it also pins exec-shaped
config such as `core.hooksPath` and `core.fsmonitor` to inert values. A boxed
agent's own in-container `git` remains normal, but config it writes into the
shared git dir cannot make the next host-side treebox call execute
repo-controlled hooks or monitors.

Your `.env`, shared caches, and scoped credential copies are mounted in. Host
isolation uses your live `~/.claude` / `~/.codex` login dirs; docker isolation
never mounts those live dirs. Instead, each harness declares which login files
to copy into a per-worktree credentials dir, refreshed on every `enter`.

## Built to be scripted

treebox assumes the caller is often another program:

- **Data → stdout, diagnostics → stderr.** Spinners and color degrade
  automatically when stderr isn't a TTY.
- **Stable exit codes** — `0` ok · `1` runtime/doctor hard-check failure ·
  `2` usage · `3` not-found · `4` auth · `5` conflict — so callers can branch
  without parsing prose.
- **`--json` with a `schemaVersion`** that only gains fields within a version
  (git-porcelain discipline), plus `--print` and `--dry-run` for scripts that
  want the commands, not the side effects.
- **Per-worktree locking**, held by `create`, `enter`, and `teardown` alike,
  so racing operations on one name — two `create fix-auth` calls, or a
  `teardown fix-auth` against a concurrent provision — conflict cleanly (`5`)
  instead of corrupting or half-removing a worktree.
