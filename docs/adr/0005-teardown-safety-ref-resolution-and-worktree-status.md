# Teardown safety, ref resolution, and worktree status

Teardown is the one destructive command, so it has to pick the right target,
refuse to lose work, and report honestly what it did and did not remove. The
decisions below cover how refs become candidates, how worktree health and
the "will I lose work?" badge are computed, how the interactive chooser
behaves, and how teardown gates, reports and exits.

## Decisions

### The main checkout is never a treebox worktree

A configured root that contains the repository (for example `root = ".."`)
makes the main checkout pass the "is under the root" filter. It is still
not a treebox worktree. Both enumerations skip it explicitly: the list rows,
so it is never listed or offered by the teardown chooser, and the ref
candidates, so `enter` and `teardown` can never resolve to the repository
itself and act on it.

Where: `cli.py` · `_collect_rows`; `resolve.py` · `candidates`

### A registration under another root alias is kept verbatim

When a registered path cannot be expressed relative to the configured root
(neither as spelled nor resolved), the registration uses some other alias
for the root. The candidate keeps that path exactly as git recorded it.
Resolving it could follow a target that has since been replaced and land in
a different tree.

Where: `resolve.py` · `Candidate.registered`

### Exact matches that are not unique are ambiguous, never guessed

Ref resolution tries an exact name match, then an exact branch match. Two
worktrees cannot share a branch, so more than one exact match should not
happen, but if it does treebox raises the ambiguity error instead of
picking one.

Where: `resolve.py` · `resolve_ref`

### One classification of worktree health

Worktree health has exactly one classification, produced by
`provision.worktree_health`:

- `healthy`: registered, linked to its own registration, record readable.
- `directory_missing`: the directory is gone (a registration may remain).
- `unregistered`: a directory git has no registration for.
- `invalid_linkage`: registered, but its `.git` pointer is missing or leads
  elsewhere, so git run inside it would act on the main checkout.
- `unreadable_state`: git is fine, but the host-owned record is missing or
  invalid.

Where: `models.py` · `Health`; `provision.py` · `worktree_health`

### Missing or mislinked worktrees are never shelled into

A registration whose directory is gone, or whose `.git` pointer no longer
leads to it, must never have git run inside it: git would crash on the
missing path or act on the main checkout. The row's `missing` flag keeps
its earlier meaning for exactly those two health values
(`directory_missing`, `invalid_linkage`), so teardown can still prune them.

The status badge for such a row is computed without touching the path. It
reads "missing · registration only" and is marked safe, because tearing it
down only prunes git's record.

Where: `cli.py` · `_collect_rows`, `_worktree_status`

### The dirty check ignores submodules

`is_dirty` runs `git status --porcelain --ignore-submodules=all`. Submodule
trees in a treebox worktree are plain copies with no `.git` at all
(`copy_submodules` strips it), so their state is not the worktree's own
dirtiness and must not block a teardown.

Where: `git.py` · `is_dirty`

### An untouched placeholder is "empty", not "merged"

A never-pushed placeholder with no commits of its own trivially passes the
ancestor check, because its tip *is* `origin/<base>`. The ancestor result is
true without anything having been merged. The badge calls this "empty": it
is still safe to delete, since nothing is lost, but it must not imply that a
PR landed.

An empty branch is styled muted. The "never pushed" caution style is for a
branch that has real, unpushed commits; with nothing at stake it would be
noise.

Where: `status.py` · `compute`

### Forge CLI calls have a 6 second timeout

Any single forge CLI call may take at most 6 seconds. After that treebox
gives up and falls back to the local (Tier-1) status for that worktree, so a
slow or hung network cannot stall the picker.

Where: `forge.py` · `_CLI_TIMEOUT`

### The forge is chosen by authentication, in a fixed order

Forge providers are probed in order (`gh`, then `glab`), and the first CLI
that is authenticated for the origin host wins. Choosing by authentication,
not by the hostname string, is what lets self-hosted GitHub Enterprise and
GitLab resolve correctly without hardcoded host patterns.

Where: `forge.py` · `_PROVIDERS`, `detect`

### GitLab merge requests: list all states, take the highest IID

`glab mr list` filters to opened requests by default, so the query passes
`--all`; without it a merged request would never surface.

Branch names are reusable, so several requests can match one source branch,
and an old merged request must not mask a newer one opened for new work.
treebox picks the request with the highest project-local IID, since IIDs
increase on creation. It does not rely on API ordering, because an old
request can have been updated more recently than a new one.

Where: `forge.py` · `GlabForge.pr_status`

### The chooser refreshes `origin` best-effort

Before computing badges, the chooser fetches `origin` so that "merged"
reflects the real `origin/<base>` and not a stale local ref. The fetch is
best-effort: it never blocks or prompts, errors are suppressed, and offline
the badges use the last fetch.

Where: `cli.py` · `_choose_worktrees`

### The chooser asks about branches per worktree

The picker is the whole teardown decision, so the branch question lives in
it too. It is a second multi-select over the chosen worktrees, not one
yes/no for the batch, because deleting a branch is a per-worktree choice:
keep one, drop another. Explicit refs take the batch-wide `--delete-branch`
flag instead.

Where: `cli.py` · `_choose_worktrees`, `_choose_branches_to_delete`,
`teardown`

### Ctrl+C on the branch question cancels the whole teardown

The prompt library maps Ctrl+C to `None`, while pressing enter with nothing
checked returns an empty list. The two are kept apart: an empty list means
"delete no branches", and `None` cancels the run with "nothing removed".
Someone who presses Ctrl+C is abandoning the decision, not answering "keep
the branches".

Where: `cli.py` · `_choose_branches_to_delete`, `_choose_worktrees`

### Number ranges are clamped before they are expanded

In the numbered fallback picker, a range such as `1-3` is clamped to
`1..n` before the range is built. A typo like `1-99999999999` must cost at
most `n` iterations; it must not hang the CLI iterating through the whole
span.

Where: `cli.py` · `_parse_selection`

### Selecting in the chooser is the confirmation; scripts never get a picker

With no refs, teardown opens the interactive chooser. Selecting there is
the confirmation, so the extra confirmation prompt is skipped. The dirty
gate is never skipped. Under `--json` or without a TTY, teardown must not
block on a picker and fails instead, the same contract as the confirmation
prompt.

Where: `cli.py` · `teardown`

### All refs are resolved before anything is removed

With explicit refs, every ref is resolved first. A typo in one of three
targets must not leave the first two half-removed.

Where: `cli.py` · `teardown`

### A lingering branch with no worktree is still a teardown target

The worktree may be gone while its branch remains, for example after a
manual `rm`. When a ref matches no worktree but is an exact local branch
name, teardown builds a candidate for the path that branch would have had,
so the stale registration is pruned and the branch can be cleaned up.

Where: `cli.py` · `teardown`

### The removal gate runs twice, and differs by how targets were chosen

The removal gate refuses a worktree with uncommitted changes (without
`--force`) or with a git worktree lock. It runs before the confirmation
prompt, so a refusal comes first, and again under the worktree lock right
before anything is removed, so work written while the prompt was open is
never deleted.

How a blocked target is treated depends on where the targets came from:

- From the chooser, a mixed selection is normal. Blocked worktrees are
  skipped, the clean ones the user picked are still removed, and treebox
  says so.
- With explicit refs it is all-or-nothing: naming a dirty tree stops the
  whole run. This is a scripting contract.

Where: `cli.py` · `teardown` (inner `_gate`), `_teardown_block`

### An incomplete cleanup is a failed teardown that can be retried

If any cleanup did not finish, teardown fails with `TEARDOWN_INCOMPLETE`
and names what remains for each worktree. Nothing that identifies the
leftovers was deleted (the worktree and its ownership record are kept), so
running the same command again retries.

Where: `cli.py` · `teardown`

### Skipped targets make the exit code a conflict, reported last

When the chooser path skipped blocked worktrees, the clean ones are gone
but the command still refused something, so it exits with the conflict
code. The exit is raised last, after the removals and the recap have been
reported.

Where: `cli.py` · `teardown`

### Volume removal is never silent

When the operator asked for volume removal and no removable volume could be
found from any source, the docker runner says "none found" instead of
printing nothing.

Where: `runners/docker.py` · `DockerRunner.teardown`

### Host teardown reports "skipped", not "cleaned"

The host runner has nothing to tear down beyond the worktree itself. No
container cleanup ran, so both the result and the note say "skipped"
(`skipped · host isolation`). They never claim that something was cleaned.

Where: `runners/host.py` · `HostRunner.teardown`
