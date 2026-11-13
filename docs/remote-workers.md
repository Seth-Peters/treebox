# Remote workers

treebox can be the isolation layer for coding-agent tasks on remote machines.
An external **coordinator** sends each task to a **worker** machine, usually
over SSH, and reads machine-readable results. On the worker, each task gets
its own worktree and runner, and parallel tasks cannot change or remove each
other's resources.

This page is the contract a coordinator builds on. It uses only the
documented CLI: [unattended mode](usage.md#unattended-use), exit codes,
`--json` payloads, `error.code`, the agent exit line and cancellation.

## Decision: the whole lifecycle runs on the worker

**Context.** A coordinator wants to run agent tasks on machines other than
its own. Two designs can do this:

1. **The whole lifecycle runs on the worker.** The coordinator runs
   `treebox` on the worker (`ssh worker treebox ...`). Provisioning, the
   runner, the host-owned record and cleanup all stay on one machine.
2. **A filesystem-transport seam.** treebox provisions the worktree on the
   coordinator's machine and executes the agent on the worker, through a
   seam that moves or translates the workspace between the two filesystems.

**Decision.** Option 1. treebox has no filesystem-transport seam, no SSH
client, no daemon and no queue.

**Why.**

- The [isolation contract](how-it-works.md#the-sandbox-config-lives-outside-the-box)
  assumes host locality: docker mounts the worktree and its git common dir at
  identical paths, the host-owned record and the runner's resource manifest
  live beside the worktree, `.git/hooks` is mounted read-only, and the
  lockfile hash reads host files. Option 1 keeps all of this true on the
  worker with no change. Option 2 must re-create each guarantee across a
  transport (two-way sync of a writable git dir, a record on one machine for
  resources on another, credentials staged across the link), which adds
  failure modes and gives nothing back.
- No requirement needs local provisioning with remote execution. Results
  leave the worker by the agent pushing from inside its sandbox, as on a
  laptop; the coordinator never needs the files. (The alternative, the
  coordinator fetching results over SSH, is not pursued.)
- Adopt a transport seam only when a concrete requirement needs the
  provisioning to happen where the agent does not run.

**Consequences.** Treebox owns worktree and runner isolation. The repository
owns its services. The coordinator owns queues, worker selection, the SSH
connection, obtaining the repository on the worker, and keeping a request
alive when the connection drops. treebox adds only what this contract cannot
be met without:

- an **absolute worktree path as an exact ref** for `enter`, `adopt` and
  `teardown`, so a retried request can never act on another task (a name ref
  falls back to a substring match, see [below](#requests-and-results));
- a docker launch whose sandbox **stopped or restarted while the agent ran**
  (an engine restart, `docker restart`, a worker shutdown) is a treebox
  failure, never an agent exit status.

Nothing else is new. The repo-scoped git token is operator template
configuration ([below](#the-repo-scoped-git-token)), with no treebox setting.

## Who owns what

| Owner | Owns |
| ----- | ---- |
| treebox | Worktrees, branches it creates, runner resources (containers, images, volumes, staged credentials), the host-owned record, the [setup and cleanup models](how-it-works.md#lifecycle-setup-and-cleanup). |
| The repository | Its services, its dependencies, its branch protection. |
| The coordinator | Task identity, queues, worker selection, the SSH connection, the clone of the repository on the worker, detached runs, retries. |
| The worker operator | The machine, the non-root worker user, treebox and git, one operator template and one treebox config (*profile*) per repository, the git token file of each repository. |

## Identity

A worktree **name** identifies a worktree only inside one worktree root on one
worker. It does not identify a task across workers or repositories.

| Identity | Owner | Form |
| -------- | ----- | ---- |
| **Task** | coordinator | A globally unique, opaque id (for example a UUID or a queue key). |
| **Worker** | coordinator | The SSH destination (or the coordinator's own worker id). |
| **Repository on the worker** | coordinator | A clone path on the worker, for example `~/repos/<repo-id>`. |
| **Worktree name** | coordinator, derived | A pure function of the task id, for example `t-<task-id>`. It is also the **branch** that `create NAME` makes and that the agent pushes, so it must be unique on the shared remote: derive it from the globally unique task id, never reuse it for another task. |
| **Worktree** | treebox | Its registered **path** on the worker (`worktree_path` in `create --json`, `path` in `list --json`). This is treebox's identity for the worktree ([paths are identity](how-it-works.md#names-are-identity-branches-are-mutable)). |
| **Runner resources** | treebox | Derived from the worktree path: container and image `treebox-<name>-<hash of path>`, label `treebox.workspace=<path>`. |

The mapping is: task id → (worker, repository clone path, worktree name)
→ worktree path on that worker → its runner resources. The coordinator
stores the worker and the worktree path for each task and addresses every
later request by that path.

Keep worktree names unique **per worker**, also across repositories: a
template's per-workspace volumes are named from the worktree name
(`${workspaceName}`).

## Setting up a worker

The worker operator does this once per machine:

1. A **non-root user** that may use the Docker engine. Docker isolation
   refuses uid 0 (`RUNNING_AS_ROOT`). Membership of the `docker` group is
   equal to root on the worker: dedicate the machine to this work.
2. `git` and treebox for that user. Check with
   `treebox --non-interactive doctor --isolation docker --json`.
3. Per repository, an **operator template** with the agent CLI
   ([customizing the sandbox](configuration.md#customizing-the-sandbox)) that
   mounts that repository's git token (see [below](#the-repo-scoped-git-token)).
4. Per repository, a **profile**: a treebox config file, for example
   `~/profiles/<repo-id>.toml`, that sets `isolation = "docker"`,
   `firewall = true` and `template = "<the repository's template>"`. The
   coordinator selects it with `TREEBOX_CONFIG` on every call for that
   repository. (This is the normal config file; treebox reads no other
   per-repository file, and never one from the repository itself.)
5. The agent's subscription login for the worker user (`~/.claude` or
   `~/.codex`). treebox stages copies of it into each sandbox, as on a laptop.
6. A git credential for the worker user's own `git clone` and `git fetch`
   (treebox's `create` fetches `origin`). A read-only token is enough.

## Who obtains the repository

**The coordinator.** `--repo` takes a path; a URL is `NOT_A_REPO`. Cloning
needs a URL policy and a fetch credential, which the coordinator and the
worker operator own, and it happens once per repository per worker, not once
per task. The coordinator clones idempotently, for example
`test -d ~/repos/<id>/.git || { git clone <url> ~/repos/<id>.tmp && mv ~/repos/<id>.tmp ~/repos/<id>; }`.
Every `create` then fetches `origin` and branches from the fresh
`origin/<base>`.

## Requests and results

Set `TREEBOX_NON_INTERACTIVE=1` (or pass `--non-interactive`) on every call.
Pass the repository with `--repo <clone path>` and the profile with
`TREEBOX_CONFIG`.

| Request | Command on the worker | Done when | Retry of the same request |
| ------- | --------------------- | --------- | ------------------------- |
| create | `treebox create <name> --repo <clone> --json` | exit `0`; store `worktree_path` | Exit `0` (a half-built worktree is finished), or `SLUG_CONFLICT` (exit `5`): the task exists - read its row from `list --json` by name (`health: healthy`, `setup: done`). `LOCK_HELD`: the first request still runs - wait and retry. Never a second worktree: the name is the task's. |
| run | `treebox enter <path> --repo <clone> -- <agent args>`, detached (below) | the run's exit status | The coordinator starts one run per run id; a duplicate run id is not started again. |
| status | `treebox list --repo <clone> --json` (row with `path`), plus the coordinator's run state | - | Read-only. |
| cancel | `SIGTERM` (or `SIGINT`, `SIGHUP`) to that run's treebox process | the run ends `CANCELLED`, exit `128 + signal` | Signal again; a finished run ignores it. |
| teardown | `treebox teardown <path> --repo <clone> --yes --json` | exit `0` | `NOT_FOUND` (exit `3`): already removed. `TEARDOWN_INCOMPLETE` (exit `1`): the worktree and record are kept - retry the same command. `DIRTY_WORKTREE` (exit `5`): the agent left uncommitted work - decide, then `--force`. |

**Address existing worktrees by path.** A name or branch ref falls back to a
unique substring: if `t-1` is already removed, `teardown t-1` removes `t-12`.
A sandboxed agent can also make a branch with any name, so a substring can
match a branch in another task's worktree. An absolute path matches only the
worktree registered at that path, so a retried request finds nothing
(`NOT_FOUND`) instead of another task.

### Detach anything that must outlive the connection

Never run a request in the foreground of an SSH session that must survive it.
When the connection drops, the remote process loses its stdout and stderr and
can die at any later write, so its result is lost or the request stops
half-way. Start each mutating request (and every run) detached on the worker,
keyed by an id the coordinator chooses, with its exit status, stdout and
stderr written to files. A retry then reads the result of the request that
already ran instead of starting it again. A small helper does this, for
example `setsid sh -c '"$@" >stdout 2>stderr </dev/null & echo $! >pid; wait $!; echo $? >status' job <command...>`
started inside a directory that `mkdir` creates (so only one start succeeds).
Store the process start time with the pid: after a worker restart the same
pid can belong to another process, and a job with no status and no live
process is **lost**.

### Telling failures apart

| What the caller sees | Means |
| -------------------- | ----- |
| `ssh` exits `255` | **Transport** failure: SSH did not run the command, or the connection dropped. Retry: every request above is safe to retry. treebox exits `255` only when it passes on an agent's exit status `255` (then with the agent line below), so read a run's status from its detached status file, never from `ssh`. |
| A detached run's status is `0` | The agent succeeded. |
| The last stderr line is `treebox: agent exited with status N` | **Agent** failure with status `N` ([agent exit codes](usage.md#agent-exit-codes)). |
| `CANCELLED`, exit `128 + signal` | The run was **cancelled**. If the coordinator did not cancel it, a shutdown of the worker did: treat it as interrupted and run again. |
| Any other non-zero status | **treebox** failure: the exit code class (`1` runtime, `2` usage, `3` not found, `4` fetch, `5` conflict) and, under `--json`, `error.code` in the one JSON object on stderr. A run (no `--json`) prints the error text. A docker sandbox that stopped or restarted while the agent ran is this class too (exit `1`, the message says so): the agent's result is unknown, so run again. |
| No status, and the run's process is gone | **Lost**: the worker restarted or the process was killed. Run again. |

## Failure behavior

- **Lost connection.** A detached request keeps running; the coordinator
  reconnects and reads its status. A request that ran in the foreground may
  have stopped at any step: send the same request again. `create` finishes a
  half-built worktree with the same settings, and `teardown` continues an
  incomplete cleanup.
- **Worker restart.** Worktrees, records and images stay on disk; containers
  are stopped; runs in progress are lost (no status) or end as `CANCELLED` or
  as a treebox failure, never as an agent exit status. A new run
  (`enter <path> -- ...`) restarts the stopped container, re-applies the
  firewall, re-stages the credentials and starts the agent again in the same
  worktree.
- **Duplicate request.** A duplicate `create` reaches the existing task
  (`SLUG_CONFLICT` or `LOCK_HELD`, then `list --json`), never a second
  worktree or branch. A duplicate `teardown` by path is `NOT_FOUND` and
  touches nothing else. Duplicate runs are prevented by the coordinator's run
  id; treebox does not stop a second `enter` of a running worktree.
- **Cancellation.** A signal to the run's treebox process stops the agent in
  both runners (in the container for docker), and the run ends `CANCELLED`
  with exit `128 + signal`. The worktree is kept, and for docker the
  container keeps running ([cancellation](usage.md#cancellation)).
- **Partial cleanup.** `TEARDOWN_INCOMPLETE` names what remains (in
  `remaining` with `--json`) and keeps the worktree, its record and the
  runner's resource manifest, so the same `teardown <path>` retries. Cleanup
  removes only resources the host-owned manifest of that one worktree names:
  a retry never removes another task's container, image or volume.

## The repo-scoped git token

Results leave the worker by the agent pushing from inside its sandbox, so the
sandbox needs a git credential: a token scoped to the one repository, supplied
by the worker operator. It is for git only. Subscription logins stay the only
auth for the agent harnesses.

**How it reaches the sandbox.** Through the repository's operator template,
with no treebox setting. The operator writes the token once to a file in its
own directory on the worker, in
[git-credential-store format](https://git-scm.com/docs/git-credential-store#_storage_format):

```bash
umask 077   # the token comes from stdin: never on a command line or in shell history
mkdir -p ~/secrets/acme-app
printf 'https://x-access-token:%s@github.com\n' "$(cat)" > ~/secrets/acme-app/credentials
```

The repository's template (`treebox template init acme-app`) mounts that
directory read-only and points git at it, in `container.json`:

```json
{
  "mounts": ["type=bind,source=/home/worker/secrets/acme-app,target=/run/secrets/git,readonly"],
  "env": {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "!f() { test \"$1\" = get && exec git credential-store --file=/run/secrets/git/credentials get; }; f"
  }
}
```

(Keep the other keys of the template's `container.json`. If the template
already sets `GIT_CONFIG_COUNT`, add the helper as the next index.) The helper
only answers `get`, so git in the sandbox never writes (`store`) or deletes
(`erase`) the file, and the mount is read-only in any case. Mount the
directory, not the file: a file mount does not see a token the operator
replaces by rename. The profile of the repository selects the template:

```toml
# ~/profiles/acme-app.toml
isolation = "docker"
firewall = true
template = "acme-app"
```

**Where the token is not.** Not in the worktree or the git common dir, not in
the image (the token directory is outside the template directory, which is
the build context: never put the token in the template directory), not in
the container's inspectable configuration (`docker inspect` shows the helper
and the mount, not the token), not on any command line (`ps`), not in the
host-owned record, the setup log, treebox's output or `--json`. The proof on
a worker checked each of these with a fake token.

**Scope.** git offers the token only to the host (and port) the file names.
Every sandbox of the repository reads the same file, so a new token in the
file reaches all of them at once, and removing the file revokes it for all of
them. The token is for git only: subscription logins stay the only auth for
the agent harnesses. Host isolation does not use the template: a host
worktree uses the worker user's own git credentials.

**What a sandboxed task can do with it.** Everything the token allows on that
repository, from inside the sandbox: read it, push any branch it is allowed
to push, also another task's branch of the same repository (all tasks of one
repository share one token), and send the token anywhere its network allows.
So:

- Use a fine-grained token for the one repository with the least
  permission (contents read and write), and a short expiry.
- Protect the default branch and release branches with branch protection or
  rulesets, so a task can only push its own task branches.
- Turn the [firewall](configuration.md#customizing-the-sandbox) on in the
  profile, so the token can only go to the allowed hosts.
- The coordinator checks that a task's branch tip is the result it expects
  before it acts on it.

## Runner guarantees and network policy

| | Host isolation | Docker isolation |
| - | -------------- | ---------------- |
| Agent runs as | the worker user, with all of its permissions | the sandbox user (never root), in a container |
| Filesystem | everything the worker user can reach, including other tasks' worktrees and the token files | its worktree, the repository's git common dir (`hooks/` read-only), shared caches, staged credential copies |
| Credentials | the worker user's live logins and git credentials | staged copies of the agent login; the repository's token directory, read-only, when its template mounts it |
| Network | the worker's network | the engine's network; with `firewall = true`, default-deny egress to an allowlist |
| Separation of parallel tasks | none beyond separate directories | separate containers and resources; shared git common dir |

Use docker isolation, with the firewall on, for tasks you do not trust. Host
worktrees share the worker's permissions and network: use them only for
trusted tasks.

## Known open risk: git settings in the shared git dir (#58)

A sandboxed task can still plant git configuration in the repository's
shared git dir (for example `core.fsmonitor`, `remote.origin.uploadpack`, a
filter in `.git/info/attributes`, or a rewritten `commondir`) that git on the
worker then runs: `treebox create` or `doctor`, or a manual `git` in the
clone. That is code execution as the worker user, who can use the Docker
engine (root on the worker) and read every task's staged token. A fix is open
in [#58](https://github.com/Seth-Peters/treebox/issues/58); its first design
did not hold. Until it is fixed, a worker operator can:

- treat a worker as one trust domain: run only tasks of the same trust level
  on it, and keep nothing on it that a task must not reach;
- use one clone per task (`~/repos/<repo-id>/<task-id>`) when tasks must not
  reach each other through the shared git dir;
- never run manual `git` in a worker's clones, and replace the clone (or the
  whole worker) after untrusted tasks;
- give the worker user only the repo-scoped tokens it needs, with a short
  expiry, and a read-only token for its own fetches.

## Related changes

These open changes are not part of this contract yet. The contract above
works without them, and each one makes a worker stronger when it merges:

- [#66](https://github.com/Seth-Peters/treebox/issues/66)
  ([PR #85](https://github.com/Seth-Peters/treebox/pull/85)) adds
  `TREEBOX_ID`, a machine-unique id derived from the registered worktree path,
  and port blocks. Then a coordinator can store the worker and `TREEBOX_ID`
  with each task; the path stays the ref.
- [#59](https://github.com/Seth-Peters/treebox/issues/59)
  ([PR #73](https://github.com/Seth-Peters/treebox/pull/73)) re-applies the
  firewall on every entry. The firewall stays off by default; the remote
  profile above turns it on. Add the git host to the template's allowlist.
- [#67](https://github.com/Seth-Peters/treebox/issues/67)
  ([PR #84](https://github.com/Seth-Peters/treebox/pull/84)) adds
  per-worktree CPU, memory and pids limits, so one task cannot starve the
  others on a worker.
- [PR #82](https://github.com/Seth-Peters/treebox/pull/82) adds an
  end-to-end evidence suite; a remote-worker scenario belongs there.
