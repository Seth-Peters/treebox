# Spec

Write the spec the user approves and the verifier proves.

1. Interview the user through the foreman for each decision the request and
   the code do not settle. Batch the questions. Record each decision in the
   user's words, and each assumption you made as an assumption.
2. Write the spec: the problem, the approach, what is out of scope, and the
   invariants of `CLAUDE.md` it must keep.
3. **Write the claims.** Each acceptance criterion is one observable claim a
   stranger can check on the real CLI:

   ```text
   `treebox teardown ghost --json` -> exit 3, stderr holds one error object
   with `error.code: NOT_FOUND`, and no worktree or branch is removed.
   ```

   A claim names the command and at least one of: exit code, JSON field,
   stream, state on disk, git state, container state. "Works correctly" is
   not a claim. For a bug, the first claim is the triage reproduction, now
   passing. Add the claims for what must not change.
4. For each new or changed route (a command, flag, error code, config key or
   environment variable), name the feature file under
   `.claude/skills/verify-treebox/features/` and the scenario that will
   cover it.
5. Commit the spec to a draft PR on the branch implementation will reuse,
   and link the issue.

Do not change product code in this stage.
