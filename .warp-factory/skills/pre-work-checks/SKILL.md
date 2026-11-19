---
name: pre-work-checks
description: The scoping checklist a factory worker settles before it starts a change. Use at the scoping gate of triage, spec and implement, to decide which asks are open.
---

# Pre-work checks

An ask is open when the request, the issue, the spec and the code do not
settle it. Send every open ask in one batch, each with your recommendation.
An ask the user already answered is closed: do not ask it again.

## Scoping checklist

1. **Change wanted.** Does the user want a change, or only an answer?
2. **Approach.** When more than one approach is reasonable, which one? The
   user can delegate the choice to you.
3. **Spec.** Does the user want a spec first? Recommend one when a decision
   needs the user, or when the change touches a contract in `CLAUDE.md`:
   exit codes, the `--json` schema, the record format, the isolation
   contract, or the invariants.
4. **Observable result.** What will the user see when the change is done: the
   command, then the exit code, JSON field, stream, or state on disk? The
   verify stage proves exactly this, so a change with no observable result is
   not ready to start.
5. **Existing work.** Is there an issue, branch or PR to adopt, not duplicate?

Trivial work (the full effect is obvious on sight) has no open asks when item
4 is clear from the request.
