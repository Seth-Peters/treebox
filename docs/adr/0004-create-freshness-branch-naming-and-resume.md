# Create: freshness, branch naming, and resume

`create` promises a worktree on fresh code, on a branch whose name says
whether a human or treebox chose it, and it must never leave a half-built
tree that a retry cannot recover. These decisions fix where each command
anchors itself, how names and branches are validated and derived, when a
fetch is required, and which leftover state `create` repairs or resumes
instead of rejecting.

## Decisions

### Every command anchors to the main worktree

The repository root is resolved to the *main* worktree, not to
`git rev-parse --show-toplevel`. Every command then sees the same worktree
set and the same root whether it was invoked from the repo, from
`.treebox/`, or from inside a linked worktree, where `--show-toplevel`
would return the linked worktree and mislead treebox.

Where: `cli.py` · `_repo_root`

### A missing git binary has its own error code

When git itself cannot run, the failure is reported as `GIT_MISSING`, a
distinct stable code. It is not a repository problem, and folding it into
the generic "not a git repo" error would send agents after the misleading
"run inside a git repo" hint.

Where: `cli.py` · `_repo_root`

### An explicit name is the branch; the directory flattens slashes

`create NAME` uses `NAME` as the branch name unchanged. Slashes stay in the
branch, and only the worktree directory flattens them
(`feature/auth` becomes `feature--auth`).

Where: `cli.py` · `create`; `models.py` · `derive_name`

### A dash-led name without `--checkout` is a usage error with a hint

Without `--checkout`, `NAME` is the branch, and a branch that starts with
`-` is never valid, so it is rejected as `INVALID_BRANCH` as always. In
practice it usually means the user ran `create -- ARGS` and the first agent
argument landed in the name slot, so the hint says how to pass agent
arguments (`treebox create NAME -- ARGS...`).

With `--checkout`, `NAME` is only the worktree name, and a dash-led name is
valid there. `create --checkout B -- -auth` is the firewall recovery command
for a `treebox/-auth` worktree, so that form must keep working.

Where: `cli.py` · `create`

### Generated branches carry the `treebox/` prefix

Branches that treebox generates start with `treebox/`. The prefix marks the
name as machine-made. The pre-push guard refuses to push such a branch, so a
placeholder can never become a PR title: the agent has to run
`git branch -m <real-name>` first.

Where: `models.py` · `PLACEHOLDER_PREFIX`

### Names are one clean lowercase token; nothing is slugified

A generated name, and each slash-separated segment of an explicit
`create NAME`, must match `^[a-z0-9-]+$`. Anything else (spaces, unicode,
dots) is a usage error. treebox does not try to slugify it into something
acceptable.

Where: `models.py` · `SLUG_RE`

### The pre-push guard lives in the worktree's private git dir

The guard hook is installed in a `treebox-hooks` subdirectory of the
worktree's private git dir. That location is private to the one worktree
(never the shared repo hooks), invisible to `git status`, and pruned
together with the worktree.

Where: `provision.py` · `_GUARD_DIR`, `install_push_guard`

### The template is resolved and parsed before any git state exists

For docker isolation, `create` resolves the template and parses the JSON it
would render (the container config, and the firewall file when the firewall
is on) before anything is provisioned. If this failed later, at container
render time, it would strand a freshly provisioned worktree and branch, and
a retried `create` would then hit `BRANCH_EXISTS`.

This check is deliberately create-only. `enter` must keep tolerating a
recorded template that has since been deleted, as long as the container
already exists.

Where: `cli.py` · `create`

### The fetch is required by default and fails loudly

Freshness is the point of `create`. A fetch of `origin` is required by
default and a failure is loud; treebox never silently falls back to stale
local refs. The only escapes are an explicit `--no-fetch`, or a repository
with no `origin` remote, where there is nothing to be fresh against.

Where: `provision.py` · `_add_worktree`

### An interactive fetch may prompt, and runs without a spinner

When a terminal is attached, the fetch tries the silent credential paths
first and, only if those fail, lets git or ssh prompt (for example for a
passphrase). No spinner runs during this fetch, so the prompt owns the
terminal cleanly, the way `git pull` behaves.

Where: `provision.py` · `_add_worktree`

### Host CLIs are probed against the real host

The credential helper for an HTTPS fetch is chosen by asking each host CLI
(`gh`, `glab`) whether it holds a credential for the actual origin host. It
is not chosen from the shape of the hostname. That is what lets self-hosted
GitHub Enterprise and GitLab resolve to the right tool.

Where: `git.py` · `_HOST_CLIS`, `_cred_config_for`

### A new branch starts from `origin/<base>`

When the branch does not exist yet, it is created from the freshly fetched
`origin/<base>` in preference to the local `<base>`, which may be stale.
The local base is used only when `origin/<base>` does not exist.

Where: `git.py` · `resolve_branch`

### A stale registration at the target path is pruned first

An interrupted teardown can remove the working directory and leave the git
registration behind. git flags such an entry as prunable, but it still holds
the branch's checkout lock, so a later `git branch -f` (the placeholder
reset) or `git worktree add` would die with a raw `GitError` and a generic
exit 1. That is the "followed `rm -rf` and still stuck" dead end. Prunable
is git's own signal that the tree is dead, so `create` prunes it and heals
itself, and notes that it did.

Where: `provision.py` · `_add_worktree`

### The branch-in-use check runs after the prune

A branch can back only one worktree, so `create` checks whether the
requested branch is already checked out elsewhere. This check is kept after
the prune so that a stale registration at the requested path is healed
before it can be mistaken for a live user of the branch. The base check
follows, because every later step assumes a resolvable base for new work.

Where: `provision.py` · `_add_worktree`, `_check_checkout_branch_in_use`,
`_check_base_exists`

### A lingering placeholder is reset only when it holds no work

Teardown keeps branches by default, so a local `treebox/<name>` placeholder
can outlive its worktree. Reusing it as is would silently bypass the
freshness invariant. A placeholder is a guaranteed-unpushed scratch ref, so
when its tip is already contained in the fresh `origin/<base>` it is safe to
move it there, and `create` does so and notes it.

A diverged placeholder carries unpushed work that must not be destroyed.
`create` resumes it, but warns loudly that this is not a fresh base and
names the command to start over (`teardown ... --delete-branch`), instead of
claiming freshness.

This applies to placeholders only. A user-named branch is never reset, and
cannot reach this code anyway: an existing user-named branch is already a
branch conflict earlier in the same function.

Where: `provision.py` · `_add_worktree`

### An existing directory is a conflict unless it is a half-built create

If the target directory already exists, `create` fails with a conflict,
with one exception: a prior run created it on this exact branch and setup
did not finish (pending or failed in the host-owned record). Launching into
that half-built tree would be the "said Ready, then failed" bug, so `create`
finishes its setup instead.

The exception is narrow on purpose. The worktree must be healthy,
registered with git on the exact expected branch, and have unfinished
setup. Anything else is a conflict.

Where: `provision.py` · `create`, `_is_resumable_unprovisioned_worktree`

### `enter` uses the exact registered path and re-reads the record

`enter` acts on the exact registered path the ref resolved to, which may be
a descendant of the configured root. It never rebuilds a path from root and
name. It also loads the record again once it holds the worktree lock,
because the setup result may have changed since the caller read the policy.

Where: `provision.py` · `enter`

### `enter` on a branch with no worktree points at `--checkout`

When a ref matches no worktree but a local or remote branch of that name
exists (the worktree was torn down, or never existed), the not-found error
carries a specific hint: run `treebox create --checkout <ref>`. That is the
command that materializes the branch, and it is more useful than the
generic miss advice.

Where: `cli.py` · `enter`

### A directory at a copy destination is detected by asking the filesystem

Before writing a managed file, treebox unlinks whatever is at the
destination. `unlink()` on a directory fails with `IsADirectoryError` on
Linux and `PermissionError` on macOS, so the errno cannot tell the cases
apart portably. treebox checks the path itself (without following
symlinks) and raises the destination conflict when it is a directory.

Where: `provision.py` · `_copyfile_no_follow`, `_is_directory_no_follow`

### Submodule content is copied from the main checkout, never fetched

treebox runs no `git submodule update`. The worktree branch's own
`.gitmodules` decides which paths exist and is never written; a path is
copied only when the main checkout also declares it and has it on disk, and
the rest are skipped with a note. A real submodule update was rejected: it
needs network and credentials per submodule and would make `create` slow and
able to fail in new ways. The cost is stated in the docs: copied submodule
content mirrors the main checkout, so it can be at another commit than the
branch pins.

Decided: issue #62, PR #77.
Where: `provision.py` · `plan_submodules`, `copy_submodules`
