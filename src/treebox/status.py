"""Collapse a worktree's git facts (+ optional PR/MR state) into one badge.

The teardown chooser answers a single question per worktree: *will I lose work
if I delete this?* So the raw facts — dirty tree, ahead/behind, upstream, merged,
PR state — are collapsed here into a short, color-coded label plus a ``safe``
flag, most-alarming signal first.

Pure logic, no subprocess: the caller gathers the facts (``git`` for Tier-1,
``forge`` for the optional Tier-2 PR state) and passes them in, which keeps this
trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .forge import PRStatus


@dataclass(frozen=True)
class WorktreeStatus:
    label: str  # rendered badge, e.g. "✎ uncommitted · ✓ merged (PR #42)"
    style: str  # rich theme style for the badge
    safe: bool  # merged (or empty) and clean — deleting loses nothing


def compute(
    *,
    dirty: bool,
    placeholder: bool,
    has_upstream: bool,
    ahead: int,
    merged_ancestor: bool,
    pr: PRStatus | None,
) -> WorktreeStatus:
    """Build the badge. ``pr`` (when present) sees squash merges that the local
    ancestor check cannot; ``merged_ancestor`` covers branches merged outside
    the PR (or with no forge answering). Either signal marks the branch merged."""
    pr_merged = pr is not None and pr.merged

    # A never-pushed placeholder with no commits of its own trivially passes the
    # ancestor check — its tip *is* ``origin/<base>`` — so ``merged_ancestor`` is
    # true without anything ever having been merged. That is *empty*, not merged:
    # deleting still loses nothing, but the badge must not imply a landed PR.
    empty = merged_ancestor and not pr_merged and (placeholder or not has_upstream) and ahead == 0
    merged = pr_merged or (merged_ancestor and not empty)

    parts: list[str] = []
    if dirty:
        parts.append("✎ uncommitted")

    if empty:
        parts.append("⚠ empty")
    elif merged:
        tag = "✓ merged"
        if pr is not None and pr.number:
            tag += f" (PR #{pr.number})"
        parts.append(tag)
    else:
        if pr is not None and pr.open:
            label = "draft" if pr.state == "draft" else "open"
            parts.append(f"● PR #{pr.number} {label}" if pr.number else f"● PR {label}")
        if placeholder or not has_upstream:
            parts.append("⚠ never pushed")
        elif ahead > 0:
            parts.append(f"⇡ ahead {ahead} · unmerged")
        elif not (pr is not None and pr.open):
            parts.append("unmerged")

    caution = (pr is not None and pr.open) or placeholder or not has_upstream or ahead > 0
    if dirty:
        style = "wt.fail"
    elif merged:
        style = "wt.ok"
    elif empty:
        # Nothing at stake — grey it out rather than raise the "never pushed"
        # caution, which is for a branch that has real, unpushed commits.
        style = "wt.muted"
    elif caution:
        style = "wt.warn"
    else:
        style = "wt.muted"

    return WorktreeStatus(
        label=" · ".join(parts), style=style, safe=(merged or empty) and not dirty
    )
