---
name: delegate
description: Hand a task to the treebox software factory, which runs it in an isolated Claude Managed Agents session and returns a draft PR. Use when someone says "delegate this", "send this to the factory", "have the factory do it", or wants a plan, request, or bug report built without working on it here.
---

# Delegate a task to the factory

The factory takes work from anyone, in any shape: a developer's finished plan,
a designer's request from Slack, a PO's one-line bug report. Your job is to
turn what you were given into one clear brief and dispatch it. You do not
implement anything here.

## 1. Shape the brief

Write the brief the way the requester would want an engineer to read it. Keep
their words; add structure only where it helps. These sections are a guide,
not a form; drop any that would be empty:

- **Goal**: what should be true when this is done
- **Context**: where in the codebase, why it matters, links or snippets they gave
- **Constraints**: what must not change, approaches to avoid
- **Done when**: how a reviewer can tell it worked

A one-line bug report can stay a few lines. A long plan can be passed through
nearly verbatim. Do not invent requirements the requester didn't express.

**Ask, don't guess, when the cost of a wrong guess is a wasted run.** If the
intent is genuinely unclear (two readings lead to different PRs), ask the
requester one or two pointed questions first. For anything bigger than a
small fix where the requester is present and wants to think it through, offer
the `grilling` skill before dispatching.

Show the requester the brief and get a yes before dispatching. A trivial,
unambiguous fix can go straight through if they asked you to just send it.

## 2. Pick the metadata

- **slug**: short kebab-case name for the work (`fix-teardown-exit-code`).
  It becomes the branch `factory/<slug>` and the session title.
- **requested-by**: who asked (a name or handle).
- **source**: where it came from (`dev`, `slack`, `issue`, `po`, ...).

## 3. Dispatch

Save the brief to a file. Long-lived plans can live in `factory/specs/<slug>.md`
(commit them yourself if they should be kept); anything else can go in a temp
file. Then run, from the repo root:

```bash
uv run --project factory factory run \
  --slug <slug> --requested-by <who> --source <where> \
  --brief-file <path>
```

Add `--dry-run` first if you want to see exactly what the worker will receive.

The command prints JSON with the `session_id`. Report it to the requester with
what happens next: the worker implements, has a fresh-context reviewer try to
break it, fixes, and opens a **draft** PR labelled `factory` on branch
`factory/<slug>`. Humans review and merge.

## If dispatch fails

The CLI exits non-zero with the reason on stderr. Common causes: no
`ANTHROPIC_API_KEY` / `ant auth login`, `factory/claude-lock.json` missing
(run `ant apply` from `factory/` first), `FACTORY_GITHUB_TOKEN` unset, or local
factory code differing from `origin/main` (the worker runs what's on `main`,
so push or pass `--allow-unpushed`). Report the cause; don't work around it.
