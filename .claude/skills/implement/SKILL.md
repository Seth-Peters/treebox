---
name: implement
description: How a treebox factory worker implements a task and opens its pull request. Use when you are the factory engineer working a delegated issue.
---

# Implement a factory task

## Set up

1. Read `CLAUDE.md` at the repo root. Its invariants (stable exit codes,
   `--json` schema discipline, never trusting the target repo's config, ...)
   bind every change, whether or not the spec mentions them.
2. Create the branch named in your first message's footer from the base it
   names (normally `origin/main`) and work only there.
3. Install the dev environment: `uv sync --extra dev`.

## Make the change

- Make the smallest change that fully meets the spec. No drive-by refactors,
  no speculative features.
- Follow the code's existing patterns: registries, typed dataclasses, the
  module boundaries described in `CLAUDE.md`.
- Add or update tests that would fail without your change.
- Keep commits scoped, with Conventional Commit subjects (`fix(cli): ...`).

Then follow the `verify` skill, then the `review` skill.

## Open the pull request

After the review loop ends, push the branch and open a **draft** PR against
the PR base named in your footer (normally `main`) with the GitHub MCP tools.
Label it `factory`, plus `factory:needs-human` if the reviewer's last verdict
was not `approve` or a verification step still fails.

PR body:

```markdown
Closes #<issue>

## What changed
<2-5 bullets>

## Assumptions
<anything you decided that the spec didn't say, or "None">

## Verification
<the evidence block from the verify skill>

## Adversarial review
Verdict: <approve | changes-requested> after <n> round(s)
<unresolved findings, verbatim, if any>

---
Factory v<version> · session `<session id>`
```
