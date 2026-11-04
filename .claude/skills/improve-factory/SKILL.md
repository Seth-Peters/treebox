---
name: improve-factory
description: The factory's outer loop. Read recent factory session transcripts and their PRs, find where the factory itself (agents, prompts, skills, environment) caused friction or failure, write a retro, and open one PR that improves the factory. Use when asked to "improve the factory", "run a factory retro", or "review how the factory is doing".
---

# Improve the factory

You are reviewing **the thing that produces the code**, not the code. The
question is never "is this PR good?" but "what about how the factory is
defined made this run go the way it did?"

Run this locally, from the repo root, in a session with `ant` and `gh`
authenticated. It's triggered by a human when enough runs have accumulated.

## 1. Find the runs since the last retro

- The newest file in `factory/retros/` names the factory version it reviewed.
  Runs to review are the factory sessions created since then.
- List sessions with `ant beta:sessions list --format jsonl` and keep those
  whose `metadata.factory_name` matches `name` in `factory/factory.yaml`. Each
  session's metadata carries `factory_version`, `factory_sha`, `slug`,
  `requested_by`, and `source`.
- If fewer than **5** new runs exist, say so and stop. Patterns need more than
  a couple of data points.

## 2. Read each run

Query the data directly; don't copy it into the repo.

- **Transcript (primary):** `ant beta:sessions:events list --session-id <id>
  --format jsonl`. The reviewer subagent runs in its own session thread; read
  its thread's events too (see `ant beta:sessions --help` for the threads
  commands). Also check `ant beta:sessions retrieve --session-id <id>` for
  status, usage, and stats.
- **PR (secondary):** `gh pr list --label factory --state all --search
  "head:factory/<slug>" --json number,state,mergedAt,labels,reviews,comments,commits`.
  Merged as-is, merged after human commits, closed unmerged, and
  `factory:needs-human` all mean different things.

## 3. Look for factory-shaped causes

Things worth finding, each tied to specific sessions:

- the engineer misunderstood briefs of a certain shape (prompt or `delegate` skill)
- time and tokens burned on environment setup or missing tools (`environments/`)
- the reviewer approved changes a human later had to fix, or blocked on noise
  (`agents/reviewer.md`, `review` skill)
- skill instructions that were ignored, contradicted, or unclear
- repeated tool errors, permission stalls, or MCP failures
- model choice: a cheaper model would have done, or a stronger one was needed

Ignore one-off product bugs that say nothing about the factory.

If `factory/scorers/` contains scorers, apply each to every run and include
the scores. (It's empty for now: scoring is a planned addition.)

## 4. Write the retro

Create `factory/retros/<YYYY-MM-DD>.md` (never edit an existing retro):

```markdown
# Factory retro <date>

Reviewed factory v<version>, <n> sessions: <session ids>

## What happened
<per run, one line: slug, outcome, notable friction>

## Patterns
<each pattern, with the session ids that show it>

## Change proposed
<the one change in this PR, the hypothesis behind it, and what you'd expect
to see in the next retro if it worked>
```

## 5. Change the factory, then open one PR

- Make **one** coherent change addressing the strongest pattern. Other
  patterns go in the retro for next time.
- You may edit what `factory_code.editable` in `factory/factory.yaml` allows
  (`factory/`, `.claude/`), never what `factory_code.never` lists: no product
  source or tests, no past retros, no hand-edits to `claude-lock.json`.
- Bump `version` in `factory/factory.yaml`: minor for a behavior change,
  patch for wording-only.
- Run `uv run --project factory factory check --boundary`. It fails if the
  diff against `origin/main` touches a forbidden path or the factory
  definition is broken. Fix the change, don't loosen the rules in the same PR.
- Branch `factory-improve/<date>`, commit, and open the PR with `gh pr create`
  labelled `factory-improvement`. The body is the retro's "Change proposed"
  section plus a link to the retro file.

Never apply the change (`ant apply`) yourself. A human merges the PR and
applies it.
