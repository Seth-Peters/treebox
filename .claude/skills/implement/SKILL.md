---
name: implement
description: How a treebox factory worker implements a task and opens its pull request. Use when you are the factory engineer working a delegated task.
---

# Implement a factory task

## Set up

1. Read `CLAUDE.md` at the repo root. Its invariants (stable exit codes,
   `--json` schema discipline, never trusting the target repo's config, ...)
   bind every change, whether or not the brief mentions them.
2. Create the task branch named in your first message from `origin/main`
   and work only there.
3. Install the dev environment: `uv sync --extra dev`.

## Make the change

- Make the smallest change that fully meets the brief. No drive-by refactors,
  no speculative features.
- Follow the code's existing patterns: registries, typed dataclasses, the
  module boundaries described in `CLAUDE.md`.
- Add or update tests that would fail without your change.
- Keep commits scoped, with Conventional Commit subjects (`fix(cli): ...`).

## Check before review

Run what `CLAUDE.md` asks for, and don't hand anything to review that fails:

```bash
uv run --extra dev ruff check src tests
uv run --extra dev ruff format --check src tests
uv run --extra dev mypy
uv run --extra dev python -m pytest
```

Run `./scripts/validate.sh` when the change touches provisioning, runners,
git handling, shell assets, or CLI output. If it fails for an environmental
reason you can't fix (no docker daemon, say), note that in the PR body rather
than skipping silently.

Then follow the `review` skill.

## Open the pull request

After the review loop ends, push the branch and open a **draft** PR against
`main` with the GitHub MCP tools. Add the label named in your first message
(`factory`), plus `factory:needs-human` if the reviewer's last verdict was
not `approve`.

PR body:

```markdown
## Brief
<the brief, verbatim>

## What changed
<2-5 bullets>

## Assumptions
<anything you had to decide that the brief didn't say, or "None">

## Adversarial review
Verdict: <approve | changes-requested> after <n> round(s)
<unresolved findings, verbatim, if any>

---
Factory v<version> · session `<session_id>`
```
