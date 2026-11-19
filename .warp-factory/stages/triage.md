# Triage

Establish the state of the work: what is asked, whether it exists already,
what causes it, and what comes next.

1. **Existing work.** Search the issues, PRs and branches for the same
   request. Adopt what exists; do not duplicate it.
2. **Scoping.** Read the `pre-work-checks` skill and find the open asks.
3. **Reproduce a bug before anything else.** Follow the `verify-treebox`
   skill: source its fixture and drive the real CLI the way the reporter
   did. Keep the command, stdout, stderr, the exit code and the resulting
   state. This failing drive is the `reproduction` output, and the verify
   stage will run the same drive after the fix. If you cannot reproduce the
   bug, report `not reproduced` with what you tried. Do not guess a cause.
4. **Cause and scope.** Trace the cause in the code. Name the modules
   touched and the invariants of `CLAUDE.md` that bear on the change.
5. **Issue.** Create or update the tracking issue with the findings and the
   reproduction, unless the work is trivial.
6. **Next.** Recommend `spec` when a decision needs the user or the change
   touches a contract (exit codes, `--json` schema, record format, isolation
   contract). Otherwise `implement`.

Do not change product code in this stage.
