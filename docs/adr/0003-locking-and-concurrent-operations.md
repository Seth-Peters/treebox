# Locking and concurrent operations

Several treebox processes, and the agents they launch, work in one
repository at the same time. Operations on the same worktree fail fast
against each other, writes to git state the whole repository shares queue
behind a waiting repository lock, and slow work (setup, container work)
always stays outside that lock. The decisions below fix what each lock
covers, in which order locks are taken, and how treebox copes with git's own
locks.

## Decisions

### The repository lock is keyed by the git common dir

The repository lock is a file, `treebox-repo.lock`, inside the git common
dir. Keying it by the common dir and not by the worktree root is what makes
one lock cover every root and every linked worktree of a repository.

Where: `locking.py` · `_REPO_LOCK_FILE`, `repo_lock`

### Lock waits are bounded, and announced only when noticeable

- A caller queues for the repository lock for at most 300 seconds
  (`REPO_LOCK_TIMEOUT`). The limit is sized for a slow `git fetch` ahead of
  it in the queue, not for the common case: the locked section normally
  takes well under a second.
- A caller queues for the ports ledger for at most 30 seconds
  (`PORTS_LOCK_TIMEOUT`). That section is a small JSON read-modify-write,
  normally milliseconds. The limit exists only to turn a stuck holder into a
  classified failure instead of a hang.
- A wait is announced once it has lasted 1 second (`_ANNOUNCE_AFTER`), so
  an uncontended lock never prints anything.

Where: `locking.py` · `REPO_LOCK_TIMEOUT`, `PORTS_LOCK_TIMEOUT`,
`_ANNOUNCE_AFTER`

### `create` holds the repository lock from the fetch to `git worktree add`

Every step from the fetch through `git worktree add` writes git state the
whole repository shares. Creates of different names hold different worktree
locks, so the repository lock is where they queue. Setup, the slow part,
runs after the lock is released and therefore concurrently across
worktrees.

Where: `provision.py` · `create`

### Ports and the record are written before the repository lock

`create` reserves the port block first, so a full range fails before
anything exists, and then writes the record copy. Both happen under the
worktree lock the caller already holds, before the repository lock (the
ports lock is never nested inside it), and before `git worktree add`. If
the add fails, the block and the record this create wrote are removed
again: nothing exists yet that they could describe.

Where: `provision.py` · `create`

### `enter` re-reads the record under the worktree lock

The caller reads the policy from the record before taking the worktree
lock. `enter` loads the record again once the lock is held, because the
setup result may have changed in between.

Where: `provision.py` · `enter`

### Small writers of shared files take the repository lock

- Installing the pre-push guard turns on `extensions.worktreeConfig`. That
  flag lives in the config every worktree shares, so the write is made under
  the repository lock. The `core.hooksPath` setting that follows is written
  to the worktree's own config file and needs no repository lock.
- Adding an ignore pattern to `info/exclude` is a read-modify-write of a
  file every worktree of the repository shares, so it is done under the
  repository lock too.

Where: `provision.py` · `install_push_guard`, `_ensure_excluded`

### Teardown takes every worktree lock before removing anything

Teardown takes the same per-worktree lock that `create` and `enter` hold. A
teardown must not delete the tree or its container out from under a
concurrent provision of the same target. For a batch, every lock is taken
before any removal starts. A held lock then aborts the whole batch cleanly
(`LOCK_HELD`, exit 5) instead of stopping it halfway through. The teardown
gate (what blocks a removal) is checked again under the locks, immediately
before removal.

Where: `cli.py` · `teardown`

### Git removal runs under the repository lock; runner cleanup does not

Removing a worktree, pruning, and deleting a branch all rewrite git state
the whole repository shares: `.git/worktrees/`, refs, and the branch's
config section. They queue behind any create or teardown doing the same.
The runner's own cleanup runs before this and stays outside the lock,
because it can be slow.

Where: `cleanup.py` · `cleanup_one`

### The record is removed last, then the port block is released

The host-owned record is removed after the worktree it describes. A failure
in an earlier step leaves the record in place, so a retried teardown still
knows the mode. The record is not shared git state, so its removal stays
outside the repository lock; the worktree lock the caller holds already
covers it.

The port block is released after the record is gone, also outside the
repository lock, because the ports lock is never nested in it. If the
release fails at that point, a stale ledger entry is left behind. The next
allocation reclaims it, so no teardown retry is needed.

Where: `cleanup.py` · `cleanup_one`

### A busy git config lock is retried

git takes the lock on a config file without waiting. A second writer fails
at once with "could not lock config file" instead of queueing; refs have a
lock timeout, config has none. treebox recognizes the failure by that
message, and runs git under `LC_ALL=C` so a translated git cannot hide it.

A config write holds its lock for microseconds, so a short retry outlasts
any live writer, for example an agent's `git push -u` or `git branch -m` in
another worktree. treebox makes up to 6 attempts with a backoff that grows
by 0.1 seconds per attempt. Only a lock file that a crashed git left behind
survives all the attempts, and the final error says so.

Where: `git.py` · `_CONFIG_LOCK_MARK`, `_CONFIG_LOCK_ATTEMPTS`,
`_CONFIG_LOCK_BACKOFF`, `worktree_add`

### A failed `worktree add` takes back only a branch it created

When `git worktree add -b` fails, treebox deletes the branch that the
failed attempt left behind. It decides ownership per attempt: only a branch
that was absent right before the attempt can be one the attempt created.
Anything already there belongs to someone else and is not touched.

Where: `git.py` · `worktree_add`
