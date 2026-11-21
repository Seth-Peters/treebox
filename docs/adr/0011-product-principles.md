# Product principles

treebox is built for agents first: it is the isolation layer a coordinator
trusts when it sends a task to another machine. Four rules follow from that
and decide many smaller questions. Each was chosen against a concrete
alternative, and the first three were the maintainer's own calls.

## Decisions

### The docker sandbox is a security boundary, not a guard rail

Until November 2026 the code and docs treated the docker sandbox as a guard
rail against accidents. The alternative was to keep it that way and turn
issues #57, #58 and #59 into documentation. It was rejected because the goal
is autonomous agents on remote workers: everything a boxed agent can write is
treated as hostile input, and a setup where the boundary cannot hold is
refused before anything runs (for example `RUNNING_AS_ROOT`).

Decided: 12 November 2026, while scoping issues #57 to #72.
Where: `runners/docker.py` · `DockerRunner.preflight`; the security
invariants in `runners/base.py`

### Explicit failure over best effort

A step that fails makes the command fail, with a classified error and a
message good enough to act on. An agent with an explicit failure can fix what
a human could; a warning it never sees helps nobody. The rejected
alternative was the earlier behaviour (a failed setup step warned and
`create` exited 0) with an opt-in `--strict`. So a failed setup step fails
`create` and `enter` (`SETUP_FAILED`, exit 1); the worktree is kept and
marked unfinished, and `enter` or the same `create` retries it. The same
rule is why an incomplete cleanup fails `teardown`. Apply it to new
behaviour: when in doubt, fail and say why.

Decided: 12 November 2026, issue #63.
Where: `cli.py` · `_classify`; `lifecycle.py` · `SetupResult`

### No backward compatibility for unreleased state

State formats that have never been released are broken freely: users rebuild.
The worktree record has one schema version. A new field is a required key of
that version, with no version bump and no upgrade path, and a worktree
without a usable record is torn down and created again (`RECORD_MISSING`,
`RECORD_INVALID`). PR #78 first shipped the opposite: an `adopt` command,
`RECORD_EXISTS`, and conversion paths, and PRs #83 to #85 each bumped the
version. All of it was removed before merge so the CLI carries no
compatibility surface, and so sandbox-writable state is never promoted to
authority by a migration.

Decided: 13 November 2026, PR #78.
Where: `record.py` · `SCHEMA_VERSION`, `WorktreeRecord`

### A boxed agent keeps full git ability; the shared git dir stays writable

Status: accepted open risk, redesign tracked in issue #88.

Committing, branching and pushing from inside the sandbox is the point of
the tool, so the repository's git common dir is mounted writable. That lets a
boxed agent write the shared `.git/config`. Two fixes were built and
measured, and both failed:

- A read-only mount on `.git/config` (the design of issue #58): a rename on
  the host detaches the mount, after which the box wrote into the real host
  config.
- A read-only common dir: it blocks every route but breaks git.
  `git switch -c <name> origin/main` exits 1 with HEAD on the old branch
  while the index and working tree have moved.

Hardening only treebox's own git calls was rejected as a denylist that will
always trail git, and a clone per sandbox with results fetched back breaks
the same-paths-on-host-and-in-the-box contract. The layout therefore stays,
the read-only `hooks/` mount and the pinned config on host-side git cover the
known routes, and issue #88 (a private git dir per sandbox) holds the real
fix. Do not retry the two read-only designs.

Decided: 13 November 2026, issues #58 and #88.
Where: `runners/docker.py` · `DockerRunner._inject`; `git.py` · `_SAFE_CONFIG`
