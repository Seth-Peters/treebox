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

Worktree commands run against the current repo; pass `--repo PATH` (absolute,
relative, or `~`-prefixed) to point elsewhere, or `--root DIR` (repo-relative,
absolute, or `~`-prefixed) to override the worktree root for this invocation.
`treebox version`, `treebox --version`, and `treebox -V` print the installed
version.

## `create` — provision and launch

```bash
treebox create                  # generated name (brave-otter), placeholder branch
treebox create fix-auth         # named up front: works on branch fix-auth
treebox create feature/auth     # slash names: branch feature/auth, dir feature--auth
treebox create --checkout feature/auth  # exact existing branch (resume work, PR review)
treebox create fix-auth -- -p "Fix the login race"  # launch the agent on a task
```

Fetches `origin`, cuts a worktree from the fresh `origin/main`, copies your
`.env` and submodules, syncs dependencies from the shared cache, records the
lockfile hash, and launches the agent.

An explicit `NAME` is intentional, so it **is the branch**, created fresh
from `origin/<base>`: slug tokens joined by slashes
(`fix-auth`, `feature/user-auth`), with the slashes flattened to `--` in the
directory. Use the flattened name when
[selecting a worktree](#enter-come-back-to-a-worktree).
The `treebox/` prefix is reserved for generated
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

Anything after `--` goes to the agent verbatim, exactly as for
[`enter`](#enter-come-back-to-a-worktree): `create NAME -- ARGS` starts the
agent on a task, and with `--json` or `--print` the args are part of the
emitted `entry_command`. Agent args need a `NAME` (a nameless `create` takes
none).

`--checkout` checks out an **existing** branch (local or `origin/`) exactly —
for resuming work or reviewing a PR. The worktree's name derives from the
branch (`feature/auth` → `feature--auth`; a placeholder drops its `treebox/`
prefix first, so `treebox/a/b` → `a--b`) unless you pass `NAME` too. It is
also the deliberate way to resume a branch you own: `create NAME` for a branch
that already exists (locally or on origin) is a loud `BRANCH_EXISTS` conflict,
never a silent reuse of old work.

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
| `--cpus N`       | CPU limit for the sandbox (`2`, `1.5`). See [resource limits](configuration.md#resource-limits). |
| `--memory SIZE`  | Memory limit in Docker's memory syntax (`512m`, `4g`).                    |
| `--pids-limit N` | Process limit (`512`). All three: docker isolation only; `--isolation host` refuses them (exit `2`). |
| `--dry-run, -n`  | Print a stable command plan; change nothing.                              |
| `--print`        | Provision, then print the launch command instead of launching.            |
| `--json`         | Provision, then print a JSON result instead of launching.                 |

A name that already exists is a loud conflict (exit `5`,
`error.code: "SLUG_CONFLICT"`) with the ways out — `enter` it, `teardown` it,
or pick another name. It is never silently reused. If a previous `create` did
not finish setup (it was interrupted, or a setup step failed), running the same
`create` again finishes setup instead; a fully provisioned same-name worktree
is still a conflict.

**A failed setup is a failed `create`.** When a setup step (a package-manager
install, a `setup_hook` command, the docker image build, the firewall, or
`postCreate`) exits non-zero, `create` exits `1` with
`error.code: "SETUP_FAILED"`. The message names the step and its exit status;
`error.path` is the file that holds the step's output
(`<worktree-parent>/.logs/<name>.setup.log`, outside the worktree). The
worktree is kept and its setup is recorded as failed, so `treebox enter <name>`
or the same `create` retries the setup. It never says ready on a failed setup.

### Submodule copies and recovery

During `create`, including retries, treebox merges checked-out submodule
contents from the source repo into the worktree. It preserves regular file
contents and permissions, including executable bits. Source symlinks, including
dangling links, are copied as links. Entries named `.git` are excluded at every
depth, and `.git` cannot be a submodule path component. Destination entries
that have no matching source entry are kept.

The worktree's own branch decides which submodules it gets. Treebox reads the
branch's `.gitmodules` and never writes that file, so a fresh worktree is clean
even when its branch renamed, added, or removed a submodule. A path is copied
only when the main checkout declares the same path and has it on disk. Treebox
skips every other path with a note (`skipped <path> · not checked out in
<repo>`, not printed under `--json`) and leaves that directory as the branch
has it. A branch without a `.gitmodules` gets no submodule trees. Treebox does
not run `git submodule update`: the copied content mirrors the main checkout,
not the commit that the branch pins.

Existing destination symlinks and hardlinks at file locations are replaced
without writing to their targets. Paths to destination directories must contain
real directories. A symlink or file in a directory path, a directory blocking a
source file or link, or a directory that changes during copying stops creation.
Treebox does not remove conflicting directories.

A copy failure identifies the affected path and gives a recovery hint. Inspect
that path, move any conflicting entry aside, then retry `create` with the same
worktree name and branch. Files copied before the failure can remain; the retry
merges the contents again. `enter` does not copy submodules. See the
[scripting contract](#scripting-against-treebox) for error codes and JSON fields.

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

Not sure what it will do? Ask first: `--dry-run` (`-n`) prints a stable plan
of the `git` / setup commands:

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

<a id="dry-run-checks"></a>

A dry run checks repository, worktree registration, and template preconditions.
It reports the corresponding `create` exit code and JSON error -
`BRANCH_EXISTS` for a name whose branch already exists,
`SLUG_CONFLICT` for an occupied worktree directory, `NOT_FOUND` for a missing
`--checkout` or base branch, `BRANCH_IN_USE` for a `--checkout` branch already
backing another worktree, `TEMPLATE_NOT_FOUND` for a docker `--template` that
doesn't resolve, `TEMPLATE_INVALID` for one that resolves but whose
`container.json` (or `firewall.json` with `--firewall`) is broken.
As with real `create`, a half-built worktree from an interrupted run previews
finishing setup (no fetch, no `worktree add`) instead of conflicting. Dry-run
verdicts reflect the refs already available locally; run `git fetch origin`
first when exact parity with create's normal fetch is required. The setup plan
detects package manifests from the exact locally available `--base` or
`--checkout` revision that it will materialize; when it resumes an interrupted
create, it detects them from the existing worktree. The submodule part of the
plan comes from the same revision's `.gitmodules` (or the existing worktree's),
with the same rule as the real run: it names each path it would copy and each
path it would skip. A dry run does not copy submodules or inspect their
destination paths, so it cannot detect
[submodule copy conflicts](#submodule-copies-and-recovery).
It also skips the runner preflight, including the
[Docker user requirement](install.md#what-each-isolation-mode-needs).
Nothing on disk or in git changes.

With `--cold --dry-run`, the cache part of the plan also matches the real cold
setup without creating its disposable cache. A host plan uses the stable
`<temporary-dir>/treebox-cold-XXXXXXXX` placeholder in cache environment
values and package-manager cache flags. A plan for a new docker container
omits the shared package-cache mounts that treebox injects and their cache
environment variables.

If an interrupted create already left a docker container behind and the Docker
daemon is reachable, the plan shows what real setup will do with it - reuse it
(or `docker start` it) instead of building and running a new one - warm or
cold. Under `--cold` it also notes that the existing container keeps its
creation-time cache mounts. This is a read-only engine query; with the daemon
down, the plan falls back to the build-and-run commands.

## `enter` — come back to a worktree

```bash
treebox enter fix-auth --harness claude
treebox enter fix-auth --harness codex -- --resume
```

`enter` and `teardown` select from registered linked worktrees below the
configured root, including nested directories. The main checkout is excluded.
They take a **ref**, matched in this order: exact worktree name, exact current
branch, then a unique substring of either. Branches are read live from git,
so a renamed branch still works. An ambiguous ref exits `2` and lists the
matches. If nested worktrees share a name, use their distinct branch refs or
set `--root` to the target's parent directory. Operations then follow the
[selected worktree path](how-it-works.md#names-are-identity-branches-are-mutable).
If the ref names a branch that exists but has no worktree (never materialized,
or torn down), the `NOT_FOUND`
error hints at `treebox create --checkout REF` instead of the generic advice.

Re-enters the worktree and launches the agent. By default it reuses the
harness the worktree was **provisioned with** (`create -H codex` then plain
`enter` launches codex, not the config default); an explicit `--harness` is a
per-session override that launches that agent this time without changing what's
recorded on disk. All recorded choices come from the worktree's
[host-owned record](how-it-works.md#the-host-owned-record), never from files a
sandboxed agent can write. The sandbox **template** is reused the same way: a worktree
created with `--template node` re-renders that template on `enter`, not the
config default, and an explicit `--template` is a per-session override. The
recorded **isolation** mode also wins over the config default; a conflicting
explicit `--isolation` exits `5` instead of entering the wrong kind of
worktree. For docker worktrees, the recorded firewall choice is reused too, so
`create --no-firewall` keeps entering cleanly even under a `firewall = true`
config.
Dependencies re-sync **only if the lockfile changed** since the last setup
finished (treebox stores the hash with the setup result in the host-owned
record; a re-sync never changes the recorded policy). If a previous setup was
interrupted or failed, `enter` runs the setup again whatever the hash, instead
of launching into a half-built tree; a setup step that fails again makes
`enter` fail with `SETUP_FAILED` (exit `1`). A worktree whose `.git` pointer is
missing or leads elsewhere is refused with `INVALID_LINKAGE` (exit `5`; the
hint points at `teardown`). Anything after `--` is passed through to the
agent. `--cold` forces a cache-bypassing re-sync.

Under docker isolation, `enter` **runs the runner preflight first** (like
`create` and `doctor`), so a stopped daemon fails fast with a clean
`DOCKER_UNAVAILABLE` error and a start-docker hint rather than a misleading
error deeper in launch. For user requirements and recovery steps, see
[Install](install.md#what-each-isolation-mode-needs).
It also **re-stages the scoped credential copies**
from the host on every entry - independent of the lockfile-hash skip - so a
host logout/revocation drops the stale copies and a fresh login reaches the
sandbox on the very next entry. The same refresh runs during `create`.
Each staged file is replaced atomically: staged symlinks and hardlinks cannot
redirect credential writes or permission changes to host files. A symlink at
the staged directory itself is rejected. Conflicting directories are preserved.
Both conditions stop `create` or `enter` with an
[`ERROR` failure](#scripting-against-treebox); inspect their contents before
removing the conflict.

The sandbox keeps the CPU, memory and process limits it was created with
(they are in the record). `enter --cpus` / `--memory` / `--pids-limit` only
restates them: a different value exits `5` (`RESOURCE_MISMATCH`) with the
exact re-create commands. A limit that came from config or the template, and
that they now give differently, is a warning with the same commands (in
`--json` mode: one entry in `warnings`, and the new values in the
`resources_drift` field). A limit a `create` flag chose never
gives this warning. See
[resource limits](configuration.md#resource-limits).

### Docker entry and firewall recovery

treebox restarts a stopped container before entry. If the container was
created with the firewall enabled, treebox runs `init-firewall.sh` on every
entry, including entries to running containers. This step occurs before the
agent starts or treebox emits a `--print` or `--json` command. It restores
rules lost after a container restart outside treebox.

If initialization fails during setup or entry, treebox exits with
`FIREWALL_FAILED` (exit `1`). It does not start the agent or emit an entry
command. The bundled script drops traffic on failure. Its allowlist fetch
then cannot succeed on another entry.

Inspect the operator template named in `error.hint`. If the hint includes a
teardown and create command, commit or back up uncommitted work before you
use it. The command checks out the current branch and preserves the worktree
path, template, firewall choice, and harness.

If the CLI cannot restore the same path and branch, the hint gives no
teardown command. Keep the worktree and repair the sandbox manually. If no
branch is available, save the current work on a branch first.

## `list` — what exists, what's stale

(`treebox ls` works too.)

```console
$ treebox list

  NAME         BRANCH          ISOLATION  LAST COMMIT            AGE  DEPS     ENV
  ──────────────────────────────────────────────────────────────────────────────────────
  fix-auth     fix/ci-caching  docker     cache uv wheels in CI   2h  ● fresh  ● present
  brave-otter  ⚠ unnamed       host       initial provision       3d  ● stale  ● present

  Summary: 2 worktrees · 1 unnamed · 1 stale
  Root: /home/me/project/.treebox/worktrees
  Rename unnamed: git branch -m <type>/<short-name>
```

Sorted by recency — the worktree you want is almost always the one that just
committed. The **name** is the stable handle; the **branch** is read live.
A worktree's **isolation** is read from its host-owned record;
worktrees without a record (for example made by an older treebox)
or worktrees with an invalid record show `unknown`
instead of being mislabeled with the current config default. The muted
**Root** footer shows the discovery root. It does not show the intermediate
directories of nested worktrees; use `list --json` for each full `path`
and for the recorded `resources` limits (`null` when the worktree has no
usable record).
On a narrow terminal, the commit-subject column is omitted first so
identity, isolation, age, dependency freshness, and `.env` state stay legible.
If name and branch still cannot fit side by side, the branch moves below its
stable name instead of either value being truncated.
A placeholder branch is always `treebox/<name>`, so its row just shows
`⚠ unnamed` — work that can't be pushed yet is visible at a glance. `stale`
means the lockfile changed since the last dependency sync — the next `enter`
fixes that automatically. A setup that did not finish shows
`◐ setup unfinished` or `✗ setup failed` in the DEPS column, and a worktree git
cannot use shows its [health](#scripting-against-treebox)
(`⚠ directory missing`, `⚠ invalid linkage`) there instead of a freshness it
cannot have.

## `teardown` — clean up

(`treebox rm` works too.)

```bash
treebox teardown                                # pick interactively (no refs)
treebox teardown fix-auth                       # remove one worktree
treebox teardown fix-auth brave-otter --force   # several at once
treebox teardown fix-auth --delete-branch       # ...and its local branch
treebox teardown fix-auth --yes --json          # unattended: confirm, keep the dirty check
```

Takes one or more refs (name, branch, or unique substring). Every ref is
resolved before anything is removed — a typo among three targets removes
nothing. Refuses to remove a worktree with uncommitted changes unless you pass
`--force`. It asks for confirmation in a terminal; unattended (`--json`,
[`--non-interactive`](#unattended-use), or no terminal) there is no prompt
and it refuses with `NEEDS_CONFIRMATION` (exit `5`) unless you pass `--yes`.
`--yes` only answers the prompt: a worktree with uncommitted changes is still
refused (`DIRTY_WORKTREE`, exit `5`); `--force` removes it anyway. For
docker-sandboxed worktrees, `--remove-volumes` also removes
per-workspace volumes authorized by the
[runner-owned manifest](how-it-works.md#freshness-is-enforced-not-hoped-for) written outside
the sandbox mount at create time, so they are still removed when the container
and user template were already deleted. If that manifest is missing or
invalid, including on older worktrees or after manual deletion of the
rendered config directory, treebox warns and skips volume removal
instead of guessing. `--skip-container` leaves containers/images untouched.
`--json` prints a structured record of what was removed (and never blocks on
a prompt).

**An incomplete cleanup is a failed `teardown`.** Teardown follows the
[cleanup model](how-it-works.md#lifecycle-setup-and-cleanup): runner resources
(container, image, volumes, sandbox files) first, then the worktree directory
and its git registration, then the ownership record. When a step cannot
finish - a container, image or volume removal fails, Docker is not reachable,
or git refuses to remove the worktree - teardown exits `1` with
`error.code: "TEARDOWN_INCOMPLETE"`, names what remains, and keeps the
worktree, its host-owned record and the runner's resource manifest. Run the
same `teardown` again when the cause is fixed: it retries the remaining steps.
`--skip-container` is the explicit way to leave docker resources alone.

Teardown checks for uncommitted changes and git locks before the prompt and
again under the worktree lock, right before it removes anything: a file written
while the confirmation prompt is open is never deleted. treebox never removes a
worktree that git refuses to remove, and never a worktree locked with
`git worktree lock` (`WORKTREE_LOCKED`, exit `5`, even with `--force`). Local
branch deletion stays best-effort: if it fails, teardown warns and reports
`branch_deleted: false` without undoing the worktree removal.

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

The badge starts with local Git facts: uncommitted changes, commits ahead of
the upstream, whether the current commit is in the base, and whether an
upstream is set. These checks work with **any** remote (GitHub, GitLab,
Bitbucket, plain git) and any auth. A freshly created worktree with no commits
of its own shows `⚠ empty` (safe to delete when clean). Its tip is already in
the base, but no work has been merged. If a forge CLI you're already logged into is present
(`gh` for GitHub, `glab` for GitLab), the picker also shows the branch's PR/MR
state. With no CLI, an unknown host, or no connection, the picker falls back
to the local badge.

A merged PR/MR proves a squash merge only when its recorded head commit
matches the worktree's current commit. New local or pushed commits on a reused
branch do not inherit the old request's green badge. If a merged request is
found but the current commit cannot be read, the commits differ, or the
request has no complete, valid head commit ID, the picker shows
`merge not verified for current tip` unless local Git independently proves
the current commit is already in the base. GitLab uses the newest request for
the branch, so an old merged request does not hide a newer open one. Dirty
worktrees remain unsafe even when their committed work has merged.

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
dirty worktrees either way.) The chooser is interactive-only: under `--json`,
`--non-interactive`, or a non-TTY, pass refs explicitly (no refs is
`NEEDS_CONFIRMATION`, exit `5`).

`teardown` is also the recovery path for a **corrupt** worktree - a registered
directory whose `.git` pointer file is gone (say, after an interrupted removal
by hand). Git commands inside such a directory silently answer for the *main*
checkout, so `create` and `enter` both refuse it loudly (exit `5`;
`error.code` is `"SLUG_CONFLICT"` for `create` and `"INVALID_LINKAGE"` for
`enter`) with a hint pointing here rather than running
setup or an agent against your real checkout. `teardown` instead verifies the
worktree's git linkage before asking whether it is dirty: a corrupt worktree is never blocked as "dirty" just because your
main checkout has uncommitted changes. It takes the normal confirmation path
instead (the interactive prompt, or `--yes` under `--json` / non-TTY), and
removal clears both the directory and git's stale registration without
touching the main checkout's files. Container cleanup survives the corruption
too: teardown reads the worktree's isolation mode from its host-owned
record, which lives beside the worktree and does not depend on the `.git`
pointer, so a corrupt docker worktree is still torn down with the runner it was
created with. Volume ownership comes separately from the host-owned resource
manifest and is never read from sandbox-writable state. Teardown removes the
record only after the worktree and its runner resources are gone.

`teardown` is also the recovery for a worktree without a usable record
(`RECORD_MISSING` / `RECORD_INVALID` on `enter`, for example one made by an
older treebox): it needs no extra flag. Without a record, a worktree that has
the runner-owned resource manifest is torn down as a docker worktree (its
container, image and manifest go too), and any other as a host worktree. Then
`treebox create` it again. The dirty-worktree and confirmation rules above
apply as usual, and so does `TEARDOWN_INCOMPLETE`: the manifest is kept until
every docker resource is gone, so the same plain `teardown` retries.

## `doctor` — is this host ready?

```bash
treebox doctor                       # checks for host isolation
treebox doctor --isolation docker    # also checks the Docker daemon
treebox doctor -H codex              # check codex instead of the config default
```

Checks git, the agent and its login, `.env`, and - because `create` requires
a fresh fetch - whether git can authenticate to `origin` without a prompt. A
missing `.env` is not a failure: the row renders as a muted `·` note marked
"optional" (the configured path still shown), since `create` simply skips the
copy. Every failing row comes with the command that fixes it.

For the **selected harness** (`--harness`, else the config default: the one
`create` launches) doctor checks what a launch needs, where the agent runs:

| Row | Host isolation | Docker isolation |
| --- | -------------- | ---------------- |
| `agent: <harness>` | The harness binary (`claude`, `codex`) is on `PATH`. | Part of the sandbox image: always `ok` here, checked in the container at launch (`HARNESS_MISSING`). |
| `login: <harness>` | A real login: the login file (`~/.claude/.credentials.json`, `~/.codex/auth.json`) holds a JSON object or, when that file is absent on macOS, Claude Code's Keychain item does. | The same login: the sandbox gets a copy of it as the login file. |

A login dir that merely exists (settings, history) is no login. The other
harnesses' login rows are shown as optional. Hard failures (`git`, `repo`,
the selected agent and its login, or the selected isolation mode) exit `1` in
both human and `--json` modes (`ok: false`), so
`treebox doctor --json && treebox create ...` works in scripts.

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
treebox template list                      # names, source, firewall support, content status, and the config default (ls works too)
treebox template path node                 # print the resolved dir: cd "$(treebox template path node)"
```

`template init <name>` copies the source template before it installs the new
directory. It refuses to replace an existing template without `--force`.
With `--force`, treebox moves the old template aside and installs the new copy
with renames. A copy failure or a failure to move the old template leaves the
old template unchanged. If installation fails, treebox renames the old template
back. If rollback also fails, the old template remains unchanged at the path
in the error message.

If cleanup fails after installation, the command succeeds and keeps the complete
new template. Human output includes a warning on stderr with the leftover path.
`--json` keeps the existing success payload and has no cleanup warning field.

Each run uses a unique directory under `$TREEBOX_HOME/templates/.treebox-tmp`.
`template init` rejects `.treebox-tmp` as a name, and `template list` hides that directory.
Treebox leaves directories from other runs untouched because another process
can still use them. Leftovers from an interrupted run can coexist with later
runs. Before you remove a leftover directory, make sure that no process still
uses it.

Then edit the `Dockerfile` and `container.json` and launch with
`treebox create <name> --isolation docker --template <name>`. If the source lacks
required files, `template init` copies it and reports the missing files.
`template list` flags any template dir missing a required file or carrying malformed JSON
before `create` does, shows whether each template can serve `--firewall`
(a `firewall.json` is optional), and marks which one is your config default. `template path [<name>]` (default: `default`)
prints the resolved location — the install-agnostic answer to "where does this
template live", including the bundled default. All three take `--json` for
scripting.

## Services in parallel worktrees

Several worktrees of one repo often need their own copy of a service: a dev
server, a database, a Compose stack. treebox does not start or manage these
services. It gives setup and the agent the identity and the port numbers a
repo-owned script needs to keep each worktree's services apart:

| Variable | Value |
| -------- | ----- |
| `TREEBOX_NAME` | The worktree name: its directory leaf, the name you pass to `enter` (`feature--auth` for branch `feature/auth`). Not unique: the same name can exist in two repos, or twice under nested roots. |
| `TREEBOX_ID` | Unique on this machine. Exactly 10 lowercase hexadecimal characters (`[0-9a-f]{10}`), derived from the registered worktree path (the first 10 characters of its SHA-256). Stable for the life of the worktree; a new worktree at the same path gets the same id. |
| `TREEBOX_PORT_BASE` | The first port of the worktree's port block, a decimal integer. |
| `TREEBOX_PORT_COUNT` | The number of ports in the block. The worktree may use `TREEBOX_PORT_BASE` through `TREEBOX_PORT_BASE + TREEBOX_PORT_COUNT - 1`. |

Both runners export them to every setup step (the detected package manager or
your `setup_hook`; the docker template's `postCreate`) and to the agent. The
`--print` and `--json` entry commands carry them too, so a replayed command
gives the agent the same values: the host command exports them before `exec`
(`sh -c 'cd … && export TREEBOX_NAME=… TREEBOX_ID=… TREEBOX_PORT_BASE=…
TREEBOX_PORT_COUNT=… && exec claude …'`), and the docker command passes them
with `-e` on `docker exec`. They are passed on every exec and not stored in
the container, so a container that was stopped and started again gets them as
well. `create --json`, `enter --json` and
`list --json` report them as `id`, `port_base` and `port_count`.

The `TREEBOX_ID` format is contract: use it where a name must be unique on the
machine and use a restricted alphabet, for example a Compose project name
(`app-$TREEBOX_ID`) or a database name. It can start with a digit, so put a
letter prefix in front when the target needs one (`app_$TREEBOX_ID` for a
PostgreSQL identifier). The docker runner's container and image are named
`treebox-<name>-<TREEBOX_ID>`.
The path is spelled the way your configured root spells it: if you later reach
the same root through another spelling (a symlink, or `/var` instead of
`/private/var` on macOS), the id changes, like the docker sandbox name.

### Port blocks

`create` reserves one block per worktree, before it makes anything: the
lowest free block of the configured range. The default range is
`20000`-`26999` in blocks of `10` (700 worktrees); set
[`[ports]`](configuration.md#port-blocks) in your user config to change it.
The block stays the same for every `enter`, across a `docker stop` and
`docker restart`, until `teardown` completes. A teardown that does not finish
(`TEARDOWN_INCOMPLETE`) keeps the block for the retry. A worktree without a
usable record (`RECORD_MISSING` or `RECORD_INVALID`) still gets its block
released by a plain `teardown`: the ledger, not the record, holds it.

Blocks never overlap across every repo and worktree root on the machine. The
reservations live in one ledger in the user-level treebox home,
`$TREEBOX_HOME/ports.json` (default `~/.treebox/ports.json`), never in a
repository; the worktree's [host-owned record](how-it-works.md#port-blocks)
keeps a copy. A full range is a classified failure before anything is made
(`PORTS_EXHAUSTED`, exit `5`, with a hint). A dry run shows the block a real
run would reserve now, and reserves nothing.

What this does **not** do:

- It prevents overlap among treebox's own allocations. It cannot reserve a
  host socket: any other program can still bind a port in a block, and a
  service that finds its port in use must fail like it would without
  treebox.
- It starts no service. Service startup, database creation and Compose stay
  in the repo's own scripts, which read the variables above.
- In docker isolation, each sandbox has its own network namespace, so its
  services cannot collide with the host or other sandboxes. The block is
  still reserved and exported, so the same scripts work in both modes.

A repo-owned `Makefile` can derive every port from the block and keep a
default for runs outside treebox:

```make
# Makefile - treebox exports TREEBOX_ID and TREEBOX_PORT_BASE.
PORT_BASE ?= $(or $(TREEBOX_PORT_BASE),3000)
APP_PORT  := $(PORT_BASE)
DB_PORT   := $(shell expr $(PORT_BASE) + 1)
DB_NAME   := app_$(or $(TREEBOX_ID),dev)

.PHONY: services env
services:  ## start this worktree's own stack
	APP_PORT=$(APP_PORT) DB_PORT=$(DB_PORT) docker compose up -d --wait

env:  ## show what this worktree uses
	@echo "app :$(APP_PORT)  db :$(DB_PORT)  database $(DB_NAME)"
```

and a `compose.yaml` that gives each worktree its own project name, so two
worktrees never share containers, networks or volumes:

```yaml
name: app-${TREEBOX_ID:-dev}
services:
  app:
    image: nginx:1.27-alpine
    ports: ["127.0.0.1:${APP_PORT}:80"]
  db:
    image: redis:7-alpine
    ports: ["127.0.0.1:${DB_PORT}:6379"]
```

Run `make services` in host isolation, from setup (`setup_hook =
["make services"]`) or from the agent. A docker sandbox has no Docker engine,
so inside a sandbox start the services as plain processes on the same ports.

## Scripting against treebox

treebox is built to be driven by other programs (including agents). Data goes
to **stdout**, diagnostics to **stderr**, and exit codes are stable:

| Code | Meaning                                                |
| :--: | ------------------------------------------------------ |
| `0`  | ok                                                     |
| `1`  | runtime failure, missing runner dependency, or failed doctor hard check |
| `2`  | usage — invalid name/branch, ambiguous ref, bad option |
| `3`  | not found — the worktree/branch/template doesn't exist |
| `4`  | required fetch or its authentication failed             |
| `5`  | conflict — name taken, dirty tree, or lock held        |

`create` and `enter` that launch the agent exit with the **agent's** exit code
instead, and a [cancel signal](#cancellation) exits `128 + signal`; see
[unattended use](#unattended-use) for how a caller tells the two apart.

The worktree commands and `doctor` take `--json`; payloads carry a
`schemaVersion` — fields are only ever added within a version, and a breaking
reshape or rename bumps it. Agents branch on these payloads, so treat the shape
as a contract. Request JSON output with:

```console
$ treebox create fix-auth --json
```

For a complete result, see the generated
[`create --json` snapshot](https://github.com/Seth-Peters/treebox/blob/main/tests/golden/create-host-json.txt).

With `--json` (or `--print`) treebox provisions but does **not** launch the
agent — it hands you the launch command instead, so your script decides when
to run it. The command is self-contained for both isolation modes: it carries
the worktree directory (`cd … && exec …` on host, `docker exec -w …` in
docker), so replaying it from any directory launches the agent in the box.
Before it prints the command, treebox completes the
[Docker entry checks](#docker-entry-and-firewall-recovery).
Without `--json`, `--print`, or `--dry-run`, `create` and `enter` launch the
agent and exit with the agent process's exit code (see
[the agent exit line](#agent-exit-codes)). Human `--dry-run` writes the
would-run plan to stderr; `--dry-run --json` writes its payload to stdout.

Current success payloads:

| Command | Top-level fields |
| ------- | ---------------- |
| `create --json`, `enter --json` | `schemaVersion`, `name`, `worktree_path`, `id`, `port_base`, `port_count`, `branch`, `base`, `entry_command`, `created`, `resources`, `warnings`, `resources_drift` (`enter` only) |
| `create --dry-run --json` | `schemaVersion`, `dry_run`, `name`, `worktree_path`, `branch`, `commands`, `resources`, `warnings` |
| `list --json` | `schemaVersion`, `worktrees` |
| `teardown --json` | `schemaVersion`, `worktrees`, `warnings` |
| `doctor --json` | `schemaVersion`, `ok`, `isolation`, `checks`, `advisories` |

`warnings` is a list of strings: every warning the run printed (or, under
`--json`, would have printed) on stderr, such as a `.env` copy skipped because
the worktree has a directory at `.env`, or a teardown step that warned. It is
`[]` when nothing was skipped, so a caller can branch on a non-empty list
instead of parsing stderr.

`list` rows contain `name`, `id`, `branch`, `unnamed`, `missing`, `last_commit`,
`commit_epoch`, `path`, `base`, `isolation`, `harness`, `deps`, `env`,
`health`, `setup`, `port_base`, and `port_count`. `id` is the [`TREEBOX_ID`](#services-in-parallel-worktrees) of the worktree; `port_base` and `port_count` are its [port block](#port-blocks), `null` when the worktree has no usable record (missing or invalid). `deps` is `fresh`, `stale`, or `unknown`: `fresh` and
`stale` only after a finished setup (a matching lockfile hash alone is never
fresh); `env` is `present` or `absent`. `setup` is the
[setup model](how-it-works.md#lifecycle-setup-and-cleanup) state - `pending`,
`failed`, or `done` - or `""` when the record is missing or invalid. `health`
is the one classification `create`, `enter`, `list` and `teardown` share:

| `health` | Meaning | `missing` |
| -------- | ------- | :-------: |
| `healthy` | Registered, linked to its own git registration, record readable. | `false` |
| `directory_missing` | The directory is gone; git may still list it. `teardown` prunes it. | `true` |
| `invalid_linkage` | Registered, but its `.git` pointer is missing or leads elsewhere. `enter` refuses (`INVALID_LINKAGE`); `teardown` recovers it. | `true` |
| `unregistered` | A directory git has no registration for (never listed; seen by `create`). | `false` |
| `unreadable_state` | Git is fine, but the host-owned record is missing or invalid (`RECORD_MISSING` / `RECORD_INVALID`). | `false` |

`missing` is kept for older consumers: it is `true` exactly when `health` is
`directory_missing` or `invalid_linkage` (only the git registration is
reliable), the same cases older treebox versions reported.

`teardown` records contain `name`, `branch`, `worktree_path`, `removed`,
`branch_deleted`, `container`, `volumes_removed`, `complete`, and `remaining`;
`container` is `cleaned`, `skipped`, or `failed`. Each field reports what the
runner actually did to that resource: `container` is `skipped` when cleanup
didn't run (`--skip-container`, or Docker unavailable) and `failed` when a
container or image removal failed, while `volumes_removed` is `true` only when
docker volumes were really removed - never on host isolation, not merely
because `--remove-volumes` was passed, and a failed volume removal leaves it
`false` without marking `container` failed. `complete` is `false` when a
cleanup step did not finish; `remaining` then names each resource that is
still present (`"<resource> (<reason>)"`) and the command exits `1`
(`TEARDOWN_INCOMPLETE`) after the payload. `removed` is `true` only when the
worktree directory was removed in this run. `doctor` checks contain `name`,
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
failure. Both submodule copy errors include `error.path` and `error.hint`.
Under `--json`, stderr holds exactly this one object: a failed step's output
is never printed there, but kept in the file `error.path` names
(`SETUP_FAILED`); `--verbose` streams step output to stderr (never stdout) if
you want it live.

`error.code` and the exit code are the scripting contract:

| `error.code` | Exit | Common trigger |
| ------------ | :--: | -------------- |
| `INVALID_CONFIG` | `2` | Bad config file or invalid `--isolation` / `--harness` / `--template` (a template name is an identifier, never a path). |
| `INVALID_NAME` | `2` | `create NAME` is not slash-separated lowercase slugs, or uses the reserved `treebox/` prefix; or a `template init` / `template path` name is not a template identifier, or `template init` names the reserved staging name. |
| `INVALID_RESOURCE` | `2` | A `--cpus` / `--memory` / `--pids-limit` (or `[resources]` / template) value treebox or the Docker engine would refuse. Nothing is provisioned. |
| `RESOURCE_UNSUPPORTED` | `2` | An explicit limit flag for a runner that cannot apply it (`--isolation host`). |
| `INVALID_BRANCH` | `2` | `create NAME` or `--checkout` names an invalid git ref. |
| `NOT_A_REPO` | `2` | `--repo` is not a git repo. |
| `INVALID_ROOT` | `2` | The worktree root (`--root` or `root` in config), or its nearest existing parent, is not a directory. Every command that takes `--root` checks it. |
| `AMBIGUOUS_REF` | `2` | A ref matches more than one worktree. |
| `NOT_FOUND` | `3` | The requested worktree, checkout branch, or base branch does not exist. |
| `FETCH_FAILED` | `4` | Required fetch/auth failed. |
| `GIT_MISSING` | `1` | git itself is not installed or not on PATH. |
| `MISSING_DEPENDENCY` | `1` | Required runner dependency is missing. |
| `DOCKER_UNAVAILABLE` | `1` | Docker is installed but the daemon is unavailable. |
| `HARNESS_MISSING` | `1` | A launch found no harness binary: not on `PATH` (host, checked before anything is provisioned) or not in the sandbox image (docker, checked at launch). `doctor` reports it as `agent: <harness>`. |
| `LOGIN_MISSING` | `1` | An [unattended](#unattended-use) launch found no usable login for the harness (the agent would wait in its own login flow). Checked before anything is provisioned; `doctor` reports it as `login: <harness>`. |
| `RUNNING_AS_ROOT` | `1` | `create` or `enter` requested Docker isolation as root (uid 0). See the [user requirement and recovery steps](install.md#what-each-isolation-mode-needs). |
| `SUBMODULE_COPY_FAILED` | `1` | An I/O or permission error prevents the submodule copy. |
| `SETUP_FAILED` | `1` | A setup step exited non-zero. The message names the step and its exit status; `error.path` is its output. The worktree is kept, marked unfinished; `enter` or the same `create` retries the setup. |
| `TEARDOWN_INCOMPLETE` | `1` | A teardown step did not finish (a container, image or volume removal failed, Docker is not reachable, or git refused to remove the worktree). The message names what remains; the worktree and its ownership record are kept, so the same `teardown` retries. The worktree keeps its [port block](#port-blocks) too. |
| `PORT_LEDGER_INVALID` | `1` | The [port-block ledger](#port-blocks) (`$TREEBOX_HOME/ports.json`, in `error.path`) cannot be read or trusted. treebox never rewrites it: fix it or move it aside (each worktree then takes its recorded block back on its next `enter`). |
| `ERROR` | `1` | Runtime failure, including a staged credential refresh failure. |
| `CANCELLED` | `128 + signal` | A [cancel signal](#cancellation) (SIGINT `130`, SIGTERM `143`, SIGHUP `129`) reached treebox while the agent ran; the agent was stopped. |
| `DESTINATION_CONFLICT` | `5` | An unsafe, conflicting, or changed path prevents the [submodule copy](#submodule-copies-and-recovery). |
| `SLUG_CONFLICT` | `5` | The worktree name is already taken, or its directory is not a healthy registered worktree - recover a registered-but-corrupt one with `treebox teardown <name>`; a stray directory git never registered is invisible to teardown and must be removed by hand (`rm -rf` it). |
| `BRANCH_EXISTS` | `5` | `create NAME` names a branch that already exists — resume it with `--checkout`. |
| `BRANCH_IN_USE` | `5` | The `--checkout` branch is already checked out in another worktree. |
| `INVALID_LINKAGE` | `5` | `enter` target's `.git` pointer is missing or does not lead to its git registration (`health: invalid_linkage`) - recover with `treebox teardown <name>`. |
| `DIRTY_WORKTREE` | `5` | Explicit teardown target has uncommitted changes (checked before the prompt and again right before removal). |
| `WORKTREE_LOCKED` | `5` | Teardown target is locked with `git worktree lock`; treebox never removes it (also with `--force`). Unlock it first. |
| `MAIN_WORKTREE` | `5` | A teardown target resolves to the repo's main working tree (possible when the worktree root contains the repo, e.g. `root = ".."`) - treebox only manages linked worktrees and never removes the repo itself. |
| `NEEDS_CONFIRMATION` | `5` | Teardown would need an interactive choice or confirmation. |
| `LOCK_HELD` | `5` | Another treebox operation holds this worktree's lock. Also raised, rarely, when the repository lock that parallel `create` / `teardown` calls queue on has been held for over five minutes, or the machine-wide port-block ledger lock for over 30 seconds: another operation is stuck, not merely busy (the message says which lock). |
| `PORTS_EXHAUSTED` | `5` | `create` (or `enter` of a worktree whose ledger entry is gone and whose recorded block is taken) finds no free [port block](#port-blocks) in the configured range. Nothing is made. Tear down worktrees you no longer need, or widen `[ports]` in the user config. `error.path` is the ledger. |
| `ISOLATION_MISMATCH` | `5` | Explicit `--isolation` disagrees with the recorded mode. |
| `RECORD_MISSING` | `5` | `enter`: the worktree has no [host-owned record](how-it-works.md#the-host-owned-record) (made by an older treebox, or the record was deleted) - `treebox teardown <name>`, then `treebox create` again. |
| `RECORD_INVALID` | `5` | `enter` / resumed `create`: the host-owned record does not parse (another `schemaVersion`, an unknown isolation mode or harness, or bound to another path) - `treebox teardown <name>`, then `treebox create` again. |
| `RECORD_CONFLICT` | `5` | A resumed `create` of a half-built worktree asks for different settings than its record holds - re-run with the recorded settings from the hint, or tear it down. |
| `RESOURCE_MISMATCH` | `5` | `enter` asks for a limit that differs from the one the sandbox was created with - drop the flag, or re-create with the commands in the hint. |
| `TEMPLATE_NOT_FOUND` | `3` | A named template doesn't exist - `template init --from` / `template path`, or `create` / `enter` provisioning (including `--dry-run`) with a template that doesn't resolve. |
| `TEMPLATE_INVALID` | `1` | A template exists but its contents can't serve the run: malformed or unreadable `container.json`, or `--firewall` with a template that has no `firewall.json`. Inspect it with `treebox template path <name>`. |
| `FIREWALL_FAILED` | `1` | The sandbox firewall failed to initialize. See [firewall recovery](#docker-entry-and-firewall-recovery). |
| `TEMPLATE_EXISTS` | `5` | `template init` names an existing template — pass `--force` to overwrite. |
| `TEMPLATE_CONFLICT` | `2` | `template init` source and destination are the same template. |
| `OVERWRITE_FAILED` | `1` | `template init --force` cannot move the existing template aside. |
| `COPY_FAILED` | `1` | `template init` cannot copy or install the template. See [template recovery](#template-scaffold-and-inspect-sandbox-templates). |

## Unattended use

A coordinator, a CI job or another agent can create, run, inspect and remove a
worktree with no terminal input. Give the global `--non-interactive` before
the command (`treebox --non-interactive create ...`), or set
`TREEBOX_NON_INTERACTIVE=1` once for the whole job. It holds **also with a
terminal attached**; without a terminal on stdin treebox already behaves this
way. In unattended mode:

- Nothing opens a picker, asks a question, or waits on the terminal.
  `teardown` without refs, or without `--yes` / `--force`, is
  `NEEDS_CONFIRMATION` (exit `5`).
- git and ssh never prompt for credentials. treebox only tries its silent
  credential paths (an ssh-agent, a credential helper, a `gh` / `glab` login,
  an HTTPS token), with git's terminal prompt off, ssh in `BatchMode`, and Git
  Credential Manager told not to open dialogs. A fetch that would need you to
  type something is `FETCH_FAILED` (exit `4`) at once. (In a terminal without
  `--non-interactive`, treebox lets git and ssh ask, like `git pull`.)
- Setup steps never read the terminal: their stdin is empty, so a step that
  asks a question gets end-of-file and fails as `SETUP_FAILED`.
- A launch checks the agent before anything is provisioned: no harness binary
  is `HARNESS_MISSING`, no usable login is `LOGIN_MISSING` (both exit `1`) -
  an agent without a login would otherwise wait in its own login flow. Run
  [`doctor`](#doctor-is-this-host-ready) first to check both.
- Subscription logins stay the only auth: treebox never reads or passes an API
  key.

### The clean flow

Create the worktree with the task, replay its entry command, inspect it, and
remove it. Every step is one command with a classified result:

```bash
export TREEBOX_NON_INTERACTIVE=1

treebox doctor --json > /dev/null              # exit 1: the agent cannot run here
out=$(treebox create fix-login --json -- -p "Fix the login race and commit")
eval "$(jq -r '.entry_command | @sh' <<<"$out")"   # runs the agent; $? is its exit code
treebox list --json | jq '.worktrees[] | select(.name == "fix-login")'
treebox teardown fix-login --yes --json        # exit 5 if the agent left uncommitted work
```

`create --json` provisions without launching and puts the agent args in
`entry_command`; check its `warnings` (for example a skipped `.env` copy).
The entry command is self-contained, so it runs the agent in the worktree from
any directory, and its exit status is the agent's. `list --json` shows the
worktree's `health` and `setup`. `teardown --yes` confirms the removal but
keeps the uncommitted-changes check: work the agent did not commit is
`DIRTY_WORKTREE` (exit `5`) and stays on disk, and `--force` removes it on
purpose.

To let treebox run the agent itself, launch directly:
`treebox create fix-login -- -p "Fix the login race and commit"` (or
`treebox enter fix-login -- ...` for an existing worktree). Then treebox
handles [cancellation](#cancellation) for you, and names the agent's exit
code as described below.

### Agent exit codes

When treebox launches the agent, it exits with the **agent's** exit code, so
an agent exit of `1` to `5` would look like a treebox failure. The two are
told apart by the last line on stderr:

- A **treebox failure** always prints its classified error (code, message and
  hint; one JSON object under `--json`) and never the line below.
- When the **agent** exits non-zero, treebox prints one final line on stderr
  with this fixed prefix, then exits with the same code:

  ```text
  treebox: agent exited with status 3
  ```

  An agent killed by a signal shows the shell-style status and the signal:
  `treebox: agent exited with status 137 (killed by SIGKILL)`. An agent exit
  of `0` prints no line.

The prefix `treebox: agent exited with status ` is a contract: match the last
stderr line against `^treebox: agent exited with status ([0-9]+)`.

### Cancellation

A cancel signal sent to the treebox process while it runs the agent stops the
agent, in both runners:

| Signal to treebox | Host isolation | Docker isolation | treebox exits |
| ----------------- | -------------- | ---------------- | :-----------: |
| `SIGINT` | Passed on to the agent process. | Passed on to the agent's process group **inside the container**. | `130` |
| `SIGTERM` | Same. | Same. | `143` |
| `SIGHUP` | Same. | Same. | `129` |

If the agent is still running 10 seconds after the signal, treebox kills it
(`SIGKILL`). treebox returns only when the agent is gone, and then fails with
`CANCELLED` (exit `128 + signal`, never the agent exit line). The worktree is
kept; for docker the **container keeps running**, so the next
`treebox enter <name>` starts the agent again at once. (Stopping the local
`docker exec` client alone would leave the agent running in the container:
treebox signals the agent itself.)

This covers the agent session that treebox launches. A replayed
`entry_command` is a plain process that treebox does not supervise: on the
host, a signal to it reaches the agent directly; for docker, stopping the
`docker exec` client does not stop the agent in the container, so launch
through treebox when you need defined cancellation. A signal while treebox
is still provisioning stops treebox: SIGINT interrupts the step, the setup
result stays `pending`, and the next `enter` finishes it.
