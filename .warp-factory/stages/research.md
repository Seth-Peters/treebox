# Research

Answer the question in your inputs. Do not change code.

1. Read `CLAUDE.md`, then the code, tests and `docs/` that bear on the
   question.
2. To see how a feature behaves, read its file in
   `.claude/skills/verify-treebox/features/`: it has the exact commands and
   the observable results.
3. Return `answered` with the findings, each with its source (file and line,
   or a link), and the open uncertainties.
4. Return `needs_worker` when the answer needs a change, a reproduction, a
   tracked issue, or more than a bounded read-only lookup.
