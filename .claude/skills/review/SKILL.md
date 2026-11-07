---
name: review
description: Get an adversarial review of the current change from the reviewer subagent, which starts with a fresh context. Use when you are the factory engineer and your change is ready for review, and again after each fix round.
---

# Adversarial review

The point of the review is independence: a reviewer who never saw your
reasoning tries to prove the change wrong. Protect that.

## Hand off

Delegate to the `reviewer` subagent. Give it only:

- the repository path and the base ref (`origin/main`)
- the spec, verbatim (your first message without the footer), plus any
  developer messages that changed it
- your verification evidence block, so it can judge whether the evidence
  actually demonstrates the spec
- on a re-review: the previous findings and which commits address them

Do **not** send your plan, your reasoning, your summary of the change, or why
you think it's correct. The reviewer reads the diff itself.

Use a fresh reviewer for the first review. For re-reviews, follow up with the
same reviewer so it can check its earlier findings were fixed.

## Act on the verdict

- **approve**: go open the PR.
- **changes-requested**: fix every `blocking` finding (and `minor` ones that are
  cheap), commit, follow the `verify` skill again, then review again.
- If you disagree with a finding, don't silently skip it: say why in your
  reply to the reviewer and let it rule.

Stop after 2 fix rounds. If the last verdict still isn't `approve`, open the
PR anyway with `factory:needs-human` and the unresolved findings in the body.
A failed run with an honest record is useful; a hidden one isn't.
