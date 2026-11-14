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
Factory skills and agent prompts are documents agents read: load the
`writing-for-agents` skill first when it is available.

## 1. Find the unreviewed runs

- A run is **reviewed** when a file in `factory/retros/` lists its session ID.
  List the reviewed IDs with
  `grep -rhoE 'sesn_[A-Za-z0-9]+' factory/retros/ | sort -u`.
  Every other session of a `factory` issue is unreviewed, whatever its
  factory version or base ref.
- Factory tasks are GitHub issues labelled `factory`:
  `gh issue list --label factory --state all --json number,title,state,createdAt,closedAt`.
- Each issue's sessions (one per attempt) come from
  `uv run --project factory factory status --issue <n>`. Every session's
  metadata carries `factory_version`, `factory_sha`, `agent_version`, `issue`,
  `branch`, and `requested_by`.
- If fewer than **5** unreviewed runs exist, say so in your reply and in the
  retro, then continue. The count limits only one kind of finding (step 3).

## 2. Read each run

- **Transcript (primary):**
  `uv run --project factory factory transcript --issue <n> --out <scratch dir>`
  writes every event in order (each message, tool call, and tool result,
  including developer steering messages) plus each subagent thread (the
  reviewer) and a `summary.json` of event and tool counts. Use a scratch
  directory outside the repo; transcripts are data, never committed.
- **Outcome (secondary):** the issue and its PR:
  `gh pr list --state all --search "<n> in:body" --json number,state,mergedAt,labels,reviews,comments,commits`.
  Merged as-is, merged after human commits, closed unmerged, and
  `factory:needs-human` all mean different things.

## 3. Look for factory-shaped causes

Things worth finding, each tied to specific sessions:

- the engineer misunderstood specs of a certain shape (`agents/engineer.md`, `implement`)
- developers had to steer mid-run for the same kind of reason (the steering
  messages are in the transcript: what did the factory keep missing?)
- verification that was skipped, shallow, or claimed without evidence (`verify-treebox`)
- time and tokens burned on environment setup or missing tools (`environments/`)
- the reviewer approved changes a human later had to fix, or blocked on noise
  (`agents/reviewer.md`, `review` skill)
- skill instructions that were ignored, contradicted, or unclear
- repeated tool errors, permission stalls, or MCP failures
- model choice: a cheaper model would have done, or a stronger one was needed

Ignore one-off product bugs that say nothing about the factory.

Sort every finding into one of two kinds:

- **Reproducible failure**: an environment, tool, script, or skill step that
  fails the same way on every run (a missing interpreter, a broken command).
  One run is enough evidence. Reproduce it before you change anything, for
  example with a probe session in the worker environment.
- **Behavior pattern**: how the engineer or reviewer chose to act (misread a
  spec, skipped a verify step, missed a bug). One run is an anecdote. With
  fewer than 5 runs showing it, record it as **seen once** and leave the
  factory unchanged for it.

For each finding, the fix lands where it can fail loudly:

| The run shows | The fix |
|---|---|
| A mistake a deterministic check could catch | A check: a `factory check` rule, a test, a `validate.sh` step. First look for an existing check that is unwired or broken; wiring it is the fix. |
| The reviewer missed a judgement-call problem | A rule in `agents/reviewer.md`. The reviewer gets only a diff, so it has room for standards; the engineer has the most context pressure. |
| The engineer could not find a file or fact | A pointer in a file the engineer already reads (`implement`, `CLAUDE.md`). |
| The engineer needed information it could not reach | Wider access: a package in `environments/`, a host in `allowed_hosts`, a tool. |
| A tool call cost much for what it returned | A cheaper command or tool. |
| A skill or prompt line did not change behavior | Delete the line (a **no-op**). |

A mechanical rule written as prose is a weak fix: prefer the check. Prose in
the engineer's prompt or skills is for steps, not standards.

If `factory/scorers/` contains scorers, apply each to every run and include
the scores. (It's empty for now: scoring is a planned addition.)

## 4. Garden

Follow the `garden` skill with the runs you just read. It asks the other
question: what about the *codebase* lets agents keep making the same mistake?
It updates `factory/garden.md` and files `garden` issues for recurring
classes; it never edits product code. Its findings go in the retro's
"Garden" section. This step is separate from the one factory change in step 6.

## 5. Write the retro

Create `factory/retros/<YYYY-MM-DD>.md` (never edit an existing retro):

```markdown
# Factory retro <date>

Reviewed <n> sessions (factory v<versions>): <every session id, including runs that showed nothing>
<if n < 5: "Below 5 runs: behavior patterns are seen once.">

## What happened
<per run, one line: slug, outcome, notable friction>

## Findings
<each finding: its kind (reproducible failure, or behavior pattern with its
run count), the session ids that show it, and where its fix lands>

## Garden
<each mistake class: evidence, times seen, level picked; creep counts and how
they changed; `garden` issues filed>

## Change proposed
<the one change in this PR, the hypothesis behind it, and what you'd expect
to see in the next retro if it worked>
```

## 6. Change the factory, then open one PR

- Make **one** coherent change for the strongest finding: a reproducible
  failure, or a behavior pattern seen in 5 or more runs. Other findings go in
  the retro for next time. When no finding qualifies, the retro alone is the
  PR: it marks the runs as reviewed. The `factory/garden.md` update from
  step 4 rides in the same PR.
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

The fix-routing table and the reproducible-versus-pattern split adapt Matt
Pocock's `retro` skill (github.com/mattpocock/skills, MIT).
