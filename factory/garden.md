# Garden queue

The gardener's memory between runs (see the `garden` skill). One row per
mistake class. A class is acted on once it has happened twice.

## Creep counts

Counted in `src/` with the commands in the `garden` skill. A count that grows
is a finding.

| Date | `# type: ignore` | `# noqa` | `cast(` | `Any` | broad `except` |
|---|---|---|---|---|---|
| 2026-11-09 | 0 | 1 | 2 | 16 | 3 |

## Classes

| Class | Evidence | Times | Level picked | Status |
|---|---|---|---|---|
| Error-code docs table is hand-synced with the code and has drifted | `COPY_FAILED` and `OVERWRITE_FAILED` are emitted (`src/treebox/cli.py:1872`, `:1887`, `:1901`) and asserted in tests, but missing from the error-code table in `docs/usage.md` | 2 | Architecture: one registry of error codes that the docs table is derived from or tested against | queued |
| An error's exit code and its `error.code` string are passed as two separate arguments | 23 `_die(...)` call sites in `src/treebox/cli.py` each pass `code=EXIT_*` and `error_code="..."` by hand. No mismatch exists today (every code maps to exactly one exit code), so the mapping is already a function that the types don't know about | 0 mistakes, 23 chances | Types: an `Enum` whose members carry their exit code, so a call site passes one value | queued (no mistake yet: do it with the registry above or leave it) |
| Several same-typed `str` parameters in a row | `Worktree.locate(repo, root, name, branch, base)` (`src/treebox/models.py:152`) takes five positional `str`s; called positionally at `provision.py:148`, `:517`, `:622` and `cli.py:1497`. No swapped-argument bug found | 0 mistakes | Types: keyword-only parameters first; `NewType` for name vs branch if a swap ever happens | queued (seen zero times: watch) |
| `Literal` values branched on with `if ==`, with no exhaustiveness check | 7 comparisons against `Literal` values (`plan.kind == "local"` in `git.py:579` and `provision.py:165`, `pr.state` in `forge.py` and `status.py`); no `match` / `assert_never` anywhere in `src/` | 0 mistakes | Types: `match` + `assert_never` where a new variant must be handled everywhere | queued (seen zero times: watch) |

Seeded on 2026-11-09 from a first scan of `src/` at `e8a7fa1`, before any
gardening run. Only the first row has happened twice.
