# Review

Review the PR at its head commit against the contract, and judge the
evidence.

1. **Check the evidence commit first.** If the verify `commit` is not the PR
   head, stop and return `human_decision_needed` with one finding, "evidence
   is stale: verified <SHA>, head is <SHA>". Do not return
   `revision_needed`: nothing in the code needs a change.
2. **Read the contract, then the diff.** Does the change do what was asked,
   all of it, and no more?
3. **Judge the evidence.** For each claim, does the recorded drive
   demonstrate it? Evidence that shows a different path, a `--dry-run` taken
   on trust, or an output with no resulting state does not. A skip that
   covers a claim means the claim is not proven. Do not repeat the drives;
   you can run one to settle a doubt.
4. **Check the invariants** of `CLAUDE.md` the diff can touch: stable exit
   codes, `--json` schema discipline, data on stdout and diagnostics on
   stderr, the isolation contract, never trusting the target repository's
   config, locks and their order.
5. **Check the tests.** Would they fail without the change?

## Verdict

- `accepted`: no finding that must be corrected.
- `revision_needed`: every open finding is unambiguous, so the worker can
  apply the correction as you wrote it.
- `human_decision_needed`: a finding needs human judgment, or a claim is not
  proven. Post those findings on the PR as a review comment, with no
  approval and no rejection, and report that you posted it.

On a re-review, check first that each earlier finding is fixed in the
commits named.
