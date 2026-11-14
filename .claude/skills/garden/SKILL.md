---
name: garden
description: Tend the treebox codebase that every factory agent shares. Find the mistakes that keep recurring in factory PRs and in the code itself, and turn each one into a check, type, or structure so it can't recur. Run by the improve-factory skill after it has read the runs; also use when asked to "garden", "find recurring mistakes", or "what should we make impossible".
---

# Garden

`improve-factory` asks what about the *factory* made a run go the way it did.
Gardening asks the other question: what about the *codebase* lets agents keep
making the same mistake? Agents copy what they find, so one tolerated shortcut
becomes the pattern. The gardener's job is not to fix a PR. It is to change
the bed so the weed can't grow back.

Assume every contributor is an agent that sees only the files it opened,
copies the nearest example, and takes the shortest path that compiles.
Design the repo so a change that looks right from one file is right for the
whole repo.

## 1. Watch: collect, fix nothing yet

Gather candidate mistakes from three places. Record each with its evidence
(PR, session ID, or `file:line`).

- **The runs `improve-factory` just read.** Reviewer findings, developer
  steering messages, "Assumptions" in PR bodies, and anything a human changed
  on a factory PR before merging it. A human fixing the same thing twice is the
  strongest signal there is.
- **A sample of merged PRs.** Read a few recent merged diffs closely:
  `gh pr list --state merged --limit 10 --json number,title,mergedAt`. Look for
  workarounds, copied blocks, and comments that explain a workaround.
- **Creep in the code.** Count these now and compare with the counts recorded
  in `.factory/garden.md`; a count that grew is a finding:

  ```bash
  grep -rnF --include='*.py' "# type: ignore" src | wc -l
  grep -rnF --include='*.py' "# noqa" src | wc -l
  grep -rnF --include='*.py' "cast(" src | wc -l
  grep -rnw --include='*.py' "Any" src | wc -l
  grep -rnE --include='*.py' "except (Exception|BaseException)" src | wc -l
  ```

## 2. Group into classes

Group the mistakes into classes. A class counts once it has happened twice.

A one-off needs no action: note it in the queue and move on. This threshold is
for mistakes in the code. Patterns in how the engineer or reviewer *behaves*
keep the 5-run threshold in `improve-factory`.

## 3. Pick the highest level that works

1. **Eliminate it with architecture.** Give each piece of state one owner
   and each task one supported way. Hide internals so the wrong import
   fails. Replace hand-synced lists with one source of truth. Delete old
   ways and dead code an agent would copy.
2. **Enforce it with types so the bad state can't be written.** If bad code
   still compiles, add a lint or CI check whose error names the file, type,
   or function to use instead. If the pattern is already common, fail only
   when a change adds more.
3. **Test the behavior.** Fix or delete any test that still passes when the
   behavior it checks breaks, or that fails when only the internals change.
4. **Write docs or agent rules last, only for judgment calls.** Nothing
   fails when an agent skips them.

If the fix is structural, only use the structural fix. The instruction is
the symptom.

For each class, write down the level you picked and why a higher level
didn't work.

### What levels 1 and 2 look like in this codebase (Python, mypy strict)

- **A value that carries its own rules, instead of two arguments kept in step.**
  When two things must always travel together (an error code and its exit
  code, a status and its label), make one value own both: an `Enum` whose
  members carry the data, or a frozen dataclass in a registry. Call sites pass
  one thing and can't mismatch them.
- **`NewType` for strings that mean different things.**
  If two arguments share a primitive type but mean different things, brand
  them.

  A worktree *name* and a *branch* are both `str` here but are different
  things (`models.py`'s name-as-identity rule). `NewType("WorktreeName", str)`
  costs nothing at runtime and makes swapping them a type error.
- **Keyword-only parameters** (`def f(repo, *, name, branch)`) for any function
  taking several values of the same type. Cheaper than `NewType`, and stops
  argument-order mistakes today.
- **`Literal` plus exhaustiveness.** Branch on a `Literal` or `Enum` with
  `match` and end with `case _: assert_never(x)`. Adding a variant then fails
  type checking at every place that forgot it, instead of falling through.
- **Derive, don't hand-sync.**
  Replace hand-synced lists with one source of truth.

  Derive a `Literal`'s values with `typing.get_args`, a registry's keys from
  the registry, and docs tables from the code (or test that they match). The
  existing drift test for `HARNESSES` / `RUNNERS` is this pattern.
- **Parse at the boundary, trust inside.**
  External data is untyped until parsed.

  JSON state files, TOML config, CLI args, `git` and `docker` output: one parse
  function each, returning a typed value (dataclass or `TypedDict`), so
  `dict[str, Any]` never travels inward.
- **A lint or CI check when types can't express it.** A ruff rule
  (`flake8-tidy-imports` `banned-api` to forbid an import or call), a mypy
  flag (`warn_unused_ignores`, `disallow_any_explicit`), or a small test that
  greps for the banned pattern. Make the error message name what to use instead.

Do not force an abstraction. Prefer boring code if the current shape is
already clear, local, and unlikely to grow.

## 4. Queue it in `.factory/garden.md`

Every class goes in the table in `.factory/garden.md`: what it is, the
evidence, how many times, the level picked, and its status (`queued`,
`issue #N`, or `enforced by <check>`). Update the creep counts there too.
This file is the gardener's memory between runs: read it first, so a class
seen once last time and once now counts as twice.

Drop a rule once its mistake can't happen.

When a class is enforced, keep its row with the check that enforces it; delete
the row only if the check itself is later removed as unnecessary.

## 5. Change the bed: file issues, don't edit the product

The fixes live in `src/`, `tests/`, and config, which the outer loop may not
touch. So the gardener's output is **GitHub issues** that go through the
factory like any other work, with verification, review, and a human merge.

For each class that has happened twice, most frequent first, at most 3 per run:

```bash
gh issue create --label garden --title "<make X impossible>" --body-file <file>
```

(Create the label once with `gh label create garden --color 5a8f3c
--description "Recurring mistake to make impossible"` if it doesn't exist.)

The body is a spec a factory worker can build from:

```markdown
## Mistake class
<what keeps going wrong, in one or two sentences>

## Evidence
<each occurrence: PR, session ID, or file:line>

## Fix
Level: <architecture | types | lint/CI check | test>
<the specific change>
Why not a higher level: <reason>

## Proof
The new check must fail on a real past mistake: <the commit, PR, or code
state to demonstrate it against>.
```

Prove each new check fails on a real past mistake.

File the issue with the `garden` label only. A developer decides whether to
delegate it (`/delegate issue <n>`); never add the `factory` label yourself.

## Reply

Each class with its evidence, the level you picked, and why a higher level
didn't work. Then the creep counts and how they changed, and the issues you
filed.
