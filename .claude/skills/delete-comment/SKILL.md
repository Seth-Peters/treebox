---
name: delete-comment
description: Delete comments (Python `#` comments and docstrings, YAML `#` comments) and lower the comment ratchet. Each comment is triaged first, so a decision moves to docs/adr/ and an excuse becomes a GitHub issue before the text is removed. Use for "/delete-comment [paths]", "delete the comments in X", "ratchet the comments down", or when scripts/comment_ratchet.py fails.
---

# Delete comments

This repo carries no comments: no `#` comment and no docstring in Python, no
`#` comment in YAML. `scripts/comment_ratchet.py` enforces it against
`scripts/comment-baseline.json`, the comment lines each file still has (a
docstring counts as the lines it spans). This skill empties part of that list without losing what the
comments knew.

Every comment in scope ends up deleted. The triage decides what is saved
first.

## 1. Scope

Take the paths the user named. With none, take the files with the highest
counts in `scripts/comment-baseline.json` until the pass covers about 300
comment lines. List them with their context:

```bash
scripts/comment_ratchet.py --list <files>
```

Read every comment together with the code it sits on. Done when each comment
in scope has been read against its code, not only its text.

## 2. Triage

Give each comment exactly one class. Ask the questions in this order; the
first yes wins.

| Class | The question | Before deleting |
| --- | --- | --- |
| **Excuse** | Does it admit the code is wrong, weaker than it looks, or a workaround? A fallback for an "impossible" case, a swallowed error, a justified `cast` / `noqa` / broad `except`, "kept for compatibility", "defense in depth" for a hole the code itself opens. | File an issue (step 4) |
| **Stale** | Does it describe code, an issue number, a dependency or a behaviour that is no longer so? Check: `gh issue view N`, `pyproject.toml`, the callers. | Nothing. If the *code* is dead too, it is an Excuse |
| **Decision record** | Would a competent reader, without it, "fix" the code and break something? It names a reason, a threat, a failure it prevents, a contract others depend on, or a rejected alternative. | Move it to an ADR (step 3) |
| **Tautology** | Everything else: section banners, a field restated in words, what the next line plainly does, a pointer to another module. | Nothing |

A docstring is triaged like a comment, sentence by sentence: the summary line
that restates the function name is a Tautology, the contract or the reason
after it is usually a Decision record. Three docstrings are code, so change
the code before deleting them:

- **A Typer command's docstring is its `--help` text.** Move it to `help=` on
  the `@app.command(...)` decorator; `scripts/golden-diff.sh` must stay clean.
- **A body that is only a docstring** (a `Protocol` method, an exception
  class) needs `...` in its place.
- **Anything read through `__doc__`**: `grep -rn "__doc__" src tests`.

A comment with two parts gets two classes (a decision plus the excuse beside
it). In a test file, a decision record is rare: a comment saying what a test
proves belongs in the test's name or docstring, which is a Tautology here
once you have renamed the test.

Done when every comment in scope has a class and every Stale claim was
checked against the repo, not assumed.

## 3. Move decisions to ADRs

Records live in `docs/adr/`, one per theme (`docs/adr/index.md` lists them).
Append each decision to the record of its theme; start `NNNN-slug.md` with
the next number only for a new theme, and add it to `docs/adr/index.md` and
the `Decisions` nav in `mkdocs.yml`.

One decision is a `###` heading that states the rule, a few sentences with
the reason and every concrete detail the comment held (numbers, flags, error
codes, the failure it prevents), then a `Where:` line naming module and
symbol. Symbols, never line numbers. Merge comments that state one decision.
If the record already has the decision, add only what is new.

Done when each Decision record comment can be found, reason intact, by
searching `docs/adr/` for its symbol.

## 4. File the excuses

One issue per hack, not per comment: group the sites that share a fix.
Search first (`gh issue list --search`), then:

```bash
gh issue create --label codebase-improvement --title "<type>(<area>): <the fix>" --body-file <file>
```

The body gives the file and symbol with a permalink to the commit that still
has the comment, quotes the comment, says what is wrong, and proposes the
fix. Leave the code as it is: the issue is the fix's home.

Done when every Excuse maps to an open issue number.

## 5. Delete, then ratchet

```bash
scripts/comment_ratchet.py --strip <files>
uv run --extra dev ruff format src tests
scripts/comment_ratchet.py --update
uv run --extra dev python -m pytest
uv run --extra docs mkdocs build --strict
```

Files under `.factory/` format with `cd .factory && uv run --extra dev ruff
format src tests`. `--strip` deletes `#` comments in Python and YAML and keeps directives
(`noqa`, `type: ignore`, `pragma:`, `fmt:`), shebangs, the version pin after
a YAML `uses:`, and `#` lines inside a YAML block scalar (the embedded
script). Docstrings it leaves: delete those by hand, per the three cases in
step 2. `--update` only lowers counts.

A change under `.github/workflows/` needs a push token with the `workflow`
scope; without one, leave those files to a human and say so in the report.

Done when `scripts/comment_ratchet.py` exits 0 and the tests and docs build
pass.

## 6. Report

Give the user: the count per class, the ADRs touched, the issues filed with
numbers, the lines removed (`git diff --shortstat`), and how many comment
lines the baseline still holds (`--update` prints it).
