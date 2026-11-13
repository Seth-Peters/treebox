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
       └─ write the host-owned record   # policy, setup pending
            └─ git worktree add   # .treebox/worktrees/<name>, on <name> (or treebox/<petname>)
                 └─ install pre-push guard     # treebox/* refs can't be pushed
                      └─ copy submodules
                           └─ copy .env
                                └─ runner.setup      # deps, from the shared cache
                                     └─ record the setup result (done + lockfile hash, or failed)
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

## Worktree paths are identity; branches are mutable { #names-are-identity-branches-are-mutable }

The full registered **path** identifies a worktree. Its **name** is the fixed
directory leaf; different parent directories can contain the same name.
After selection, `enter` and `teardown` keep that full path for provisioning,
state access, runner operations, and reporting. See
[worktree selection](usage.md#enter-come-back-to-a-worktree) for ref matching.
The Docker sandbox mounts the absolute path and a live agent's working
directory is there, so the worktree directory is never renamed. The
**branch** is a mutable attribute.
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
files (lockfiles) when setup finishes and stores the hash, with the setup
result, in the [host-owned record](#the-host-owned-record). That is what lets
`enter` re-sync dependencies only when they actually changed, and `list` show
`fresh` / `stale` at a glance. The record also says whether setup finished:
`enter` finishes an interrupted or failed setup instead of skipping it as
unchanged.

The setup result is host-owned on purpose. The worktree's private git dir
(`.git/worktrees/<id>/`) is sandbox-writable, because the Docker runner mounts
the git common directory; a sandboxed agent that could write "setup done" there
could make treebox skip its setup forever or report stale dependencies as
fresh. treebox therefore reads nothing from that directory for any decision.

## Lifecycle: setup and cleanup

Two small models decide what `create`, `enter`, `list` and `teardown` do. Each
lives in one place (`src/treebox/lifecycle.py`); the commands derive their
decisions from it.

**Setup.** The record holds the result of the last setup attempt:

| State | Meaning | Recovery |
| ----- | ------- | -------- |
| `pending` | Setup has not finished: not started yet, or interrupted. | `enter`, or the same `create`, runs setup again. |
| `failed` | A setup step exited non-zero. The record keeps the step and its exit status; its output is in `<worktree-parent>/.logs/<name>.setup.log`. | `enter`, or the same `create`, runs setup again. |
| `done` | Setup finished, against the recorded lockfile hash. | `enter` runs setup again only when the lockfile hash changed. |

Transitions: `create` writes `pending` before the worktree exists, then `done`
or `failed`. `enter` moves `pending` or `failed` (or `done` with a changed
lockfile hash) to `pending`, runs setup, then records `done` or `failed`.
A failed setup is a failed command (`SETUP_FAILED`,
exit `1`); an interrupted one stays `pending`. `list` reports `deps: fresh` or
`stale` only from `done`.

**Cleanup.** `teardown` removes one worktree's resources in a fixed order:

1. Runner resources: container, image, volumes (with `--remove-volumes`), and
   the sandbox files that hold the resource manifest.
2. The worktree: its directory and its git registration.
3. Ownership: the host-owned record and the setup log.

A step runs only when every earlier step finished. Each resource a step could
not remove is named in `remaining`. While anything remains, the worktree, its
record and the resource manifest are kept, and the teardown fails
(`TEARDOWN_INCOMPLETE`, exit `1`); the same command retries, because nothing
that names the remaining resources was deleted. Before any step, under the
worktree lock, teardown checks again for uncommitted changes and for a git
lock; git's own refusal to remove a worktree is final, with no fallback that
deletes it anyway.

**Health** is classified once, for every command: `healthy`,
`directory_missing`, `invalid_linkage` (the `.git` pointer is missing or
leads elsewhere, so git inside the directory would act on the main checkout),
`unregistered`, or `unreadable_state` (record missing or invalid). See
[`list --json`](usage.md#scripting-against-treebox) for how the older `missing`
field maps to it.

### The host-owned record

The worktree's **creation policy** - isolation, firewall, sandbox template,
harness, and base - lives in a host-owned record outside every sandbox mount:

```text
<worktree-parent>/.records/<name>.json
```

Here, `<worktree-parent>` is the selected worktree's immediate parent, which
can be below the configured discovery root (the same parent that holds
`.locks/` and `.containers/`). `create` writes the record atomically, under the
worktree lock, before any runner resource depends on it. The record is
versioned (`schemaVersion`) and bound to the registered worktree path it
describes. `enter` reads the policy only from this record: it reuses the
recorded isolation, firewall, and template, and the recorded harness unless a
per-session `--harness` is passed; a per-session `--template` is the
operator's override for one session. An explicit `--isolation` that disagrees
with the recorded mode is a conflict, not an override. `teardown` takes the
isolation mode from the record and removes the record only after the
worktree and its runner resources are gone.

A sandboxed agent therefore cannot choose host execution, drop its firewall, or
point its next sandbox at a template it wrote: none of that is in files it can
reach. A resumed `create` (an earlier setup did not finish) must restate the
recorded policy, so a resume can never turn a plain `create NAME` into a policy
change (`RECORD_CONFLICT`).

**A worktree without a usable record is recreated.** A worktree made by an
older treebox has no record, and a record that does not parse (another
`schemaVersion`, an unknown isolation mode or harness, or bound to another
path, for example after the worktree root was moved) is invalid. `enter`
refuses both with `RECORD_MISSING` / `RECORD_INVALID` (exit `5`) and launches
nothing; it never falls back to the config default or to the sandbox-writable
state. `list` still shows the worktree, with an
unknown isolation. Fix it with `treebox teardown <name>`, then
`treebox create` again (`--checkout <branch>` keeps its branch). Teardown
needs no record: without one, a worktree that has the runner-owned resource
manifest below is torn down as a docker worktree, and any other as a host
worktree. The manifest is kept until every docker resource is gone, so an
incomplete teardown of such a worktree is retried the same way.

**Host-isolation worktrees** get the same record. They have no sandbox, so the
agent can reach the record like any other host file; the record is not a
boundary against a host-run agent, which already has your full user
privileges. It gives both modes one code path, so the mode of a worktree is
never read from its git state and a host worktree is never silently treated
as anything else.

Before first Docker setup, the runner also writes exact per-workspace image
and volume ownership to
`<worktree-parent>/.containers/<name>/treebox-resources.json`, outside every
sandbox mount. `teardown --remove-volumes` validates and trusts only that
manifest, so it can still remove volumes when the container and user template
are both gone.

If an operator manually deletes the rendered config directory (and with it the
manifest) of a docker worktree that still has its record, the worktree stays
tear-downable, but image and volume removal are skipped with a warning:
treebox does not guess ownership from volume names, current templates,
container mounts, or sandbox-writable state.

## Warmth lives in the cache, not the tree

treebox detects the package manager from the repo — **uv, npm, pnpm, go, or
cargo** — and drives its cache-backed install. Installs hardlink out of shared
host caches (`~/.cache/uv`, the pnpm store, …) that are reused across
worktrees *and* mounted into containers. A tenth worktree costs seconds, not a
re-download. `--cold` bypasses the caches when you want a from-source build.
A package manager that is not installed is skipped with a note. A setup step
that fails - in host isolation or in the sandbox - fails the command
(`SETUP_FAILED`); see [the setup model](#lifecycle-setup-and-cleanup).

## The sandbox config lives outside the box

Docker isolation's threat model is simple: **an agent must not be able to
edit the config that defines its own sandbox.**

- The container template (Dockerfile, `container.json`, firewall setup) is
  **operator-owned** — bundled with treebox or copied to your
  `~/.treebox/templates/`, never read from the target repo.
- A template **name** is an identifier (letters, digits, `.`, `_`, `-`), never
  a path: an absolute path or `..` is refused, so a name can only select a
  directory under the operator-owned locations (`$TREEBOX_TEMPLATE_DIR`, then
  `~/.treebox/templates/<name>`, then the bundled `default`).
- The choice of template is in the [host-owned record](#the-host-owned-record),
  not in sandbox-writable state.
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
  want the commands, not the side effects. An emitted entry command is
  entry-ready: a stopped docker sandbox is restarted (firewall re-applied)
  before the command is printed, never dead on replay.
- **Unattended mode** (`--non-interactive`, also with a terminal attached):
  no picker, question or credential prompt; what would need an answer is a
  classified failure. When treebox launches the agent it passes the agent's
  exit code through and names a non-zero one in a fixed final stderr line, and
  a cancel signal stops the agent in both runners - inside the container for
  docker. See [unattended use](usage.md#unattended-use).
- **Per-worktree locking**, held by `create`, `enter`, and `teardown` alike,
  so concurrent operations on the same worktree conflict (exit `5`). Separate
  worktrees with the same name have separate locks. Explicit batch teardown
  acquires all target locks before removal; a lock conflict removes nothing.
