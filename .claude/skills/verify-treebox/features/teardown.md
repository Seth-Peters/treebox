# Teardown

Teardown removes worktrees: the runner's resources, the directory, the git registration, optionally the branch, then the record and the port block. It asks first on a terminal, refuses without confirmation when unattended, keeps uncommitted work unless forced, and never removes the main checkout or a git-locked worktree. An incomplete cleanup keeps what a retry needs.

Used with: [Create](./create.md) made what it removes, [List](./list.md) shows what is left, [Docker sandbox](./docker-sandbox.md) adds the container, image and volumes, [Identity and ports](./identity-and-ports.md) gets the port block back, [Unattended use](./unattended.md) is the no-prompt contract, [Remote workers](./remote-workers.md) retry it by path.

## Sub-features

- `teardown-confirm`: unattended, `--yes` or `--force` is required; `--yes` alone still refuses uncommitted changes.
- `teardown-force`: `--force` removes a dirty worktree; the branch is kept unless `--delete-branch`.
- `teardown-many`: several refs in one call, each with its own result.
- `teardown-refs`: refs resolve as in enter; an absolute path is exact; the original name still resolves after a branch rename.
- `teardown-refusals`: the main checkout and a git-locked worktree are never removed.
- `teardown-corrupt`: a worktree without its `.git` pointer is removed and its registration pruned.
- `teardown-prompt`: on a terminal, a confirmation prompt, and with no refs a chooser with status badges.
- `teardown-output`: `--json` is a record per worktree; `--quiet` and `--verbose` change only stderr.

## How to get to it (user POV)

- `treebox teardown [REFS...]`, alias `treebox rm`.
- Flags: `--repo PATH`, `--root PATH`, `--yes/-y`, `--force`, `--delete-branch`, `--remove-volumes`, `--skip-container`, `--json`, `--quiet/-q`, `--verbose/-v`.
- No refs on a terminal: the interactive chooser.

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`. Create `clean-td` and `fix-auth` (`treebox create NAME --repo "$REPO" --root "$ROOT" --json`). No engine is needed; docker cleanup is in [Docker sandbox](./docker-sandbox.md).

- **No confirmation.** Run `treebox teardown clean-td --repo "$REPO" --root "$ROOT" </dev/null`. Exit 5, stderr `Refusing to remove non-interactively without confirmation.` and a hint that names `--yes` and `--force`. With `--json`: exit 5 `NEEDS_CONFIRMATION`. `$ROOT/clean-td` still exists.
- **Yes keeps the dirty check.** Run `touch "$ROOT/fix-auth/junk.txt"`, then `treebox teardown fix-auth --repo "$REPO" --root "$ROOT" --yes --json`. Exit 5 `DIRTY_WORKTREE`, `error.path` `$ROOT/fix-auth`.
- **Yes.** Run `treebox teardown clean-td --repo "$REPO" --root "$ROOT" -y --json`. Exit 0. stdout `{"schemaVersion": 1, "worktrees": [{"name": "clean-td", "removed": true, "branch_deleted": false, "complete": true, "remaining": [], …}], "warnings": []}`. `$ROOT/clean-td` is gone.
- **Force after a rename.** Run `git -C "$ROOT/fix-auth" branch -m fix/auth-renamed`, then `treebox teardown fix-auth --repo "$REPO" --root "$ROOT" --force --json`. Exit 0, the record has `name` `fix-auth` and `branch` `fix/auth-renamed`. `git -C "$REPO" branch --list 'fix/*'` still prints `fix/auth-renamed`.
- **Delete the branch.** Create `del-me`, then `treebox teardown del-me --repo "$REPO" --root "$ROOT" --force --delete-branch --json`. Exit 0, `branch_deleted` true; `git -C "$REPO" branch --list del-me` prints nothing.
- **Many.** Create `m-one` and `m-two`, then `treebox teardown m-one m-two --repo "$REPO" --root "$ROOT" --force --json`. Exit 0, `worktrees` has two records. Explicit refs are all-or-nothing: with `b-one` clean and `b-two` dirty (`touch "$ROOT/b-two/x"`), `treebox teardown b-one b-two --repo "$REPO" --root "$ROOT" --yes --json` exits 5 `DIRTY_WORKTREE` and `b-one` stays too.
- **No refs, unattended.** `treebox teardown --repo "$REPO" --root "$ROOT" --json </dev/null` exits 5 `NEEDS_CONFIRMATION`, `Refusing to choose a worktree non-interactively.`
- **Alias.** Create `rm-me`, then `treebox rm rm-me --repo "$REPO" --root "$ROOT" --force --json`. Exit 0, removed.
- **Exact path.** Create `abs`. `treebox teardown "$ROOT/ab" --repo "$REPO" --root "$ROOT" --force --json` exits 3 `NOT_FOUND` and `abs` stays. `treebox teardown "$ROOT/abs" --repo "$REPO" --root "$ROOT" --force --json` exits 0 and removes it.
- **Corrupt worktree.** Create `corrupt`, `rm "$ROOT/corrupt/.git"`, and dirty the main checkout: `touch "$REPO/main-dirt.txt"`. `treebox teardown corrupt --repo "$REPO" --root "$ROOT" --json` exits 5 `NEEDS_CONFIRMATION`, not `DIRTY_WORKTREE`. With `--force --json`: exit 0, `git -C "$REPO" worktree list` no longer names it, and `$REPO/main-dirt.txt` is still there (remove it after).
- **Main checkout.** Run `git -C "$REPO" branch repo`, then `treebox teardown repo --repo "$REPO" --root "$SBX" --force --json`. Exit 5 `MAIN_WORKTREE`, `$REPO` intact.
- **Locked.** Create `locked`, run `git -C "$REPO" worktree lock "$ROOT/locked"`, then `treebox teardown locked --repo "$REPO" --root "$ROOT" --force --json`. Exit 5 `WORKTREE_LOCKED`, the hint gives the `git … worktree unlock …` command. After that unlock, the same teardown exits 0.
- **Quiet and verbose.** `treebox teardown q-one --repo "$REPO" --root "$ROOT" --force --quiet` leaves stderr empty. `--force --verbose` on another worktree prints the steps, `container  n/a (host isolation)`, `worktree  removed …`, `ports  released …`, `branch  kept …` (the default prints them too), plus the raw output of the commands it runs.
- **Prompt on a terminal.** Create `i-keep` and `i-yes`. On macOS, `script -q /dev/null CMD` gives a command a pseudo-terminal. `(sleep 3; printf 'n\r') | script -q /dev/null treebox teardown i-keep --repo "$REPO" --root "$ROOT"` shows `Remove worktree i-keep? [y/N]:`, exits 1 with `Aborted.`, and `i-keep` stays. The same with `printf 'y\r'` for `i-yes` exits 0 and removes it.
- **Chooser.** `(sleep 3; printf ' '; sleep 0.5; printf '\r'; sleep 2; printf '\r') | script -q /dev/null treebox teardown --repo "$REPO" --root "$ROOT"` opens `Select worktrees to tear down` with one row per worktree and a status badge (`✓ merged`, `empty`), ticks the first row, confirms, skips the branch question, and exits 0 with only that worktree removed. `printf '\003'` (Ctrl+C) instead removes nothing.

## Gotchas

- Without a terminal on stdin, teardown never prompts: a test that forgets `--yes` or `--force` gets exit 5, not a hang. Give `</dev/null` when you want to prove that.
- `--yes` is only the prompt. A dirty tree needs `--force`.
- In `--json`, a host worktree reports `container` `cleaned`, while the human output says `n/a (host isolation)`.
- Linux `script` takes the command with `-c`: `script -qc 'treebox teardown …' /dev/null`. Under `script` the chooser prints a `cursor position requests (CPR)` warning; it is harmless.
- The chooser's merged badge needs the forge; the fixture's origin is a local path, so the boundary scenario R46 uses a fake `gh`.
- In the chooser, a dirty pick is kept (`kept · uncommitted changes`) while the clean picks are removed, and the exit is 5 (I5). A branch whose worktree directory is already gone is still a target: its record says `removed` false, exit 0.
- Another operation on the same worktree makes teardown exit 5 `LOCK_HELD`; a substring that matches two worktrees is exit 2 `AMBIGUOUS_REF`.
- `TEARDOWN_INCOMPLETE` (exit 1) needs a failing engine step; it is driven in [Docker sandbox](./docker-sandbox.md) and by H10.
- Scenarios: E1, E2, E3, E4, E5, E6, E7, E8, E9, E10, E11, C18, I1, I2, I3, I4, I5, R46.
