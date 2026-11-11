---
name: delegate
description: Hand a developer's spec or plan to the treebox software factory, which files a GitHub issue for it and runs it in an isolated Claude Managed Agents session that implements, verifies, reviews, and opens a draft PR. Also steers or checks a running factory task. Use when a developer says "delegate this", "send this to the factory", "have the factory build it", or wants to steer or check on a factory run.
---

# Delegate to the factory

The developer comes with the spec or plan already worked out. That's their
job, not the factory's. Yours is to get it to the factory intact, then help
them follow and steer the run.

## Reading the request

Developers invoke this as `/delegate ...` or in plain words. Map what they say
to arguments before doing anything else:

| They say | Arguments |
|---|---|
| "issue 38", "#38" | `--issue 38` (skip writing a brief: the issue body is the brief) |
| "from my branch", "from my current branch", "on this branch" | `--ref $(git branch --show-current)` |
| "dry run", "show me first" | `--dry-run` |
| nothing about a branch | no `--ref`: the worker runs the factory from `main` |

So `/delegate issue 38 from my current branch` is:

```bash
uv run --project factory factory delegate --issue 38 --ref "$(git branch --show-current)"
```

Run it directly; no confirmation is needed for an existing issue.

`--ref` also sets the PR base: the worker branches from that ref and opens its
PR against it, not against `main`. When you report back after a `--ref` run,
say which branch the PR will target.

## Hand off a task

1. **Confirm the spec.** Use what the developer wrote, verbatim where you can.
   If it's scattered across the conversation, assemble it into one markdown
   brief (what to build, where, constraints, what "done" looks like), show it,
   and get a yes. Don't invent requirements. A short issue title goes with it.
2. **Dispatch.** Save the brief to a temp file and run, from the repo root:

   ```bash
   uv run --project factory factory delegate --title "<title>" --brief-file <path>
   ```

   To delegate an issue that already exists, run
   `uv run --project factory factory delegate --issue <n>` instead.

   This files the GitHub issue (labelled `factory`), starts the worker session,
   and comments the session ID on the issue. Add `--dry-run` first to see
   exactly what the worker will receive.
3. **Report back** the issue URL and session ID from the JSON output, and what
   happens next: the worker implements, verifies end to end, has a
   fresh-context reviewer try to break it, fixes, and opens a draft PR that
   closes the issue.

## Steer a running task

When the developer notices something wrong or wants to add context mid-run:

```bash
uv run --project factory factory steer --issue <n> "<their message>"
```

It reaches the running session as a developer message and is recorded on the
issue. Pass the developer's words, not a paraphrase.

## Check on a task

```bash
uv run --project factory factory status --issue <n>           # sessions + status
uv run --project factory factory status --issue <n> --wait    # block until it settles
```

## If something fails

The CLI exits non-zero with the reason on stderr. Common causes: not logged in
(`ant auth login`), `factory/claude-lock.json` missing (run `ant apply .` in
`factory/` and commit it), `FACTORY_GITHUB_TOKEN` unset, or `gh` not logged in.
Report the cause; don't work around it.

**"Local factory code differs from origin/…"** means the worker would run
different factory instructions than the ones checked out here. The worker
always runs the factory from the branch it checks out (`--ref`, default `main`):

- If the developer is on a pushed branch that changes the factory (testing
  unmerged factory work), rerun with `--ref <that branch>`. The error message
  names the branch when this applies.
- If the factory changes aren't pushed yet, ask the developer to push them.
- Don't offer `--allow-unpushed` as a fix. It runs the worker on the other
  branch's factory code, so the run doesn't test what the developer has. Use it
  only when the developer explicitly asks for it.
