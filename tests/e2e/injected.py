"""Scenario Z1: a deliberate failure, registered only by ``--inject-failure``.

It proves the evidence command's failure path with the real machinery: a
failed scenario is recorded in the bundle, shown in the summary, and makes the
command exit 1. It is never part of a normal run.
"""

from __future__ import annotations

from harness import Ctx, group, scenario


@group("Z", "injected failure (harness self-test)")
def _setup(ctx: Ctx) -> None:
    return


@scenario("Z1", "injected failure: expects an exit status treebox never returns")
def z1(ctx: Ctx) -> None:
    run = ctx.tb("version")
    ctx.exit_is(run, 99)
