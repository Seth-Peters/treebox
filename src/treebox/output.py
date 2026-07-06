"""Terminal output — a deliberately polished, Rich-backed experience.

Design goals: one consistent visual rhythm across every command, semantic color
(green = good / fresh, yellow = caution / stale, red = failure, dim = secondary),
aligned status rows, and clean spinners that resolve into a checkmark. Everything
degrades gracefully off-TTY and under ``NO_COLOR`` (Rich handles both): no color,
no animation, but the same readable layout.

Convention: data → stdout (``data_console``); progress and diagnostics → stderr
(``console``). ``--json`` callers construct a ``quiet`` reporter so nothing but
the JSON reaches stdout.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import IO

from rich import box
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.padding import Padding
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .harnesses import HARNESSES
from .models import TemplateRow, WorktreeRow

# Style A — uv/Astral-minimal: mostly dim + default, one colored glyph per line,
# right-aligned dim timings. The only saturated color is the status glyph
# (green ✓ / red ✗) and the freshness dots in `list`.
THEME = Theme(
    {
        "wt.ok": "green",
        "wt.fail": "bold red",
        "wt.warn": "yellow",
        "wt.muted": "dim",
        "wt.name": "default",  # status-row labels: calm, unbold
        "wt.label": "bold",  # reserved for headers
        "wt.accent": "cyan",
        "wt.detail": "dim",  # step detail text
        "wt.time": "dim",  # right-aligned per-step timing
        "wt.fresh": "green",
        "wt.stale": "yellow",
        "wt.unknown": "dim",
    }
)

# Status rows are indented two columns so the glyph sits at column 2; the spinner
# is padded to the same column so a live ``⠸`` and a resolved ``✓`` line up exactly.
_INDENT = 2
# Width the status-row label column is padded to, for a clean left edge.
_LABEL_W = 14
# Right margin kept clear of the terminal edge for the timing column.
_RIGHT_MARGIN = 2
_OK = "✓"
_FAIL = "✗"
_DOT = "·"
_BULLET = "●"


def format_elapsed(seconds: float) -> str:
    """Human, compact elapsed time: ``8ms`` under a second, ``1.2s`` above."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def format_age(seconds: float) -> str:
    """Compact humanized age for the list AGE column: ``42s``, ``5m``, ``3h``,
    ``2d``, ``5w`` — one unit, no noise."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    if seconds < 14 * 86400:
        return f"{seconds / 86400:.0f}d"
    return f"{seconds / (7 * 86400):.0f}w"


def new_console(*, stderr: bool) -> Console:
    return Console(stderr=stderr, theme=THEME, highlight=False, soft_wrap=False)


@dataclass(frozen=True)
class DoctorCheck:
    """One doctor checklist row, plus the optional remediation advisory a
    failing slow check surfaces under the list."""

    name: str
    ok: bool
    detail: str
    advisory: str | None = None


class StepError(RuntimeError):
    """A progress step failed; carries the captured command output."""

    def __init__(self, label: str, returncode: int, log: str) -> None:
        super().__init__(f"{label} failed (exit {returncode})")
        self.label = label
        self.returncode = returncode
        self.log = log


class Reporter:
    """Renders the human-facing experience. One per command invocation."""

    def __init__(
        self,
        *,
        quiet: bool = False,
        verbose: bool = False,
        silent: bool = False,
        stream: IO[str] | None = None,
    ) -> None:
        # silent: emit nothing at all (used in --json mode, where the CLI writes
        # the structured result/error to stdout/stderr itself). Implies quiet,
        # and additionally suppresses errors/warnings that quiet keeps.
        self.silent = silent
        self.quiet = quiet or silent
        self.verbose = verbose
        self.console = Console(stderr=True, theme=THEME, highlight=False, file=stream)
        self.data_console = new_console(stderr=False)

    # --- headers & summaries -------------------------------------------------

    def heading(self, title: str, subtitle: str = "") -> None:
        """A spaced, intentional command header: ``● create   feature/auth``."""
        if self.quiet:
            return
        text = Text("  ")
        text.append(_BULLET + " ", style="wt.accent")
        text.append(title, style="wt.label")
        if subtitle:
            text.append("   ")
            text.append(subtitle, style="wt.muted")
        self.console.print()
        self.console.print(text)
        self.console.print()

    def summary(self, label: str, value: str) -> None:
        """A dim key / value line under a heading."""
        if self.quiet:
            return
        text = Text("    ")
        text.append(f"{label:<{_LABEL_W}}", style="wt.muted")
        text.append("  ")
        text.append(escape(value), style="wt.detail")
        self.console.print(text)

    def blank(self) -> None:
        if not self.quiet:
            self.console.print()

    def ready(self, total: str, harness: str) -> None:
        """The Style-A closing line: green ``Ready``, dim ``in <t> — launching <harness>``."""
        if self.quiet:
            return
        text = Text("  ")
        text.append("Ready", style="wt.ok")
        text.append(f" in {total} — launching {harness}", style="wt.muted")
        self._print(text)

    # --- status rows ---------------------------------------------------------

    def _row(
        self,
        symbol: str,
        symbol_style: str,
        label: str,
        label_style: str,
        detail: str,
        detail_style: str,
        timing: str | None = None,
    ) -> Text:
        text = Text("  ")
        text.append(f"{symbol} ", style=symbol_style)
        # Pad to the column, then always keep two spaces before the detail so an
        # exactly-column-width label never collides with its detail.
        text.append(f"{label:<{_LABEL_W}}", style=label_style)
        if detail:
            text.append("  ")
            text.append(escape(detail), style=detail_style)
        self._append_timing(text, timing)
        return text

    def _append_timing(self, text: Text, timing: str | None) -> None:
        """The signature Style-A move: a dim, right-aligned timing column. Off-TTY
        we omit it entirely rather than pad ANSI into a pipe."""
        if not (timing and self.console.is_terminal):
            return
        width = self.console.width or 80
        pad = width - text.cell_len - len(timing) - _RIGHT_MARGIN
        text.append(" " * max(2, pad))
        text.append(timing, style="wt.time")

    def _print(self, text: Text) -> None:
        # soft_wrap: never hard-wrap a status row mid-line (which would drop the
        # indent on the continuation); let the terminal handle overflow.
        self.console.print(text, soft_wrap=True)

    def ok(self, label: str, detail: str = "", timing: str | None = None) -> None:
        if not self.quiet:
            self._print(self._row(_OK, "wt.ok", label, "wt.name", detail, "wt.detail", timing))

    def note(self, label: str, detail: str = "") -> None:
        if not self.quiet:
            self._print(self._row(_DOT, "wt.muted", label, "wt.muted", detail, "wt.muted"))

    def fail(self, label: str, detail: str = "failed") -> None:
        if not self.silent:
            self._print(self._row(_FAIL, "wt.fail", label, "wt.fail", detail, "wt.fail"))

    # --- plain messages ------------------------------------------------------

    def info(self, msg: str) -> None:
        if not self.quiet:
            self._print(Text("  " + msg, style="wt.muted"))

    def warn(self, msg: str) -> None:
        if self.silent:
            return
        text = Text("  ")
        text.append("! ", style="wt.warn")
        text.append(escape(msg), style="wt.warn")
        self._print(text)

    def error(self, msg: str) -> None:
        if self.silent:
            return
        text = Text("  ")
        text.append(f"{_FAIL} ", style="wt.fail")
        text.append(escape(msg), style="wt.fail")
        self._print(text)

    def hint(self, msg: str) -> None:
        """A dim, indented follow-up that says exactly how to fix the error."""
        if self.silent:
            return
        text = Text("    ")
        text.append("↳ ", style="wt.muted")
        text.append(escape(msg), style="wt.muted")
        self._print(text)

    def command(self, cmd: str) -> None:
        """Render a would-run command (``--dry-run``): comments dim, commands
        with a faint cyan prompt."""
        text = Text("    ")
        if cmd.startswith("#"):
            text.append(escape(cmd), style="wt.muted")
        else:
            text.append("$ ", style="wt.accent")
            text.append(escape(cmd), style="wt.detail")
        self._print(text)

    def restore_terminal(self) -> None:
        # Rich restores the cursor itself when a status/live context exits; kept
        # for API compatibility with callers in the error path.
        self.console.show_cursor(True)

    # --- spinner-backed work -------------------------------------------------

    def _spinner(self, label: str, *, style: str, indent: int) -> Padding:
        """An indented spinner whose glyph lands at column ``indent``, so a live
        ``⠸ label`` and the ``✓ label`` row it resolves into share a left edge."""
        spinner = Spinner("dots", text=Text(label, style=style), style="wt.accent")
        return Padding(spinner, (0, 0, 0, indent))

    @contextmanager
    def _status(self, label: str) -> Iterator[None]:
        if self.quiet or self.verbose or not self.console.is_terminal:
            yield
            return
        with Live(
            self._spinner(label, style="wt.muted", indent=_INDENT),
            console=self.console,
            transient=True,
            refresh_per_second=12.5,
        ):
            yield

    @contextmanager
    def task(self, label: str, detail: str = "done") -> Iterator[None]:
        """Spinner around an arbitrary block; resolves to a ✓ row (or ✗ on error),
        with the block's elapsed time shown in the dim right-aligned column."""
        start = time.monotonic()
        try:
            with self._status(label):
                yield
        except Exception:
            self.fail(label)
            raise
        else:
            self.ok(label, detail, timing=format_elapsed(time.monotonic() - start))

    def step(
        self,
        label: str,
        detail: str,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Run ``argv`` as a status step. Output is hidden on success and shown,
        cleanly framed, on failure (or streamed live in verbose mode)."""
        start = time.monotonic()
        if self.verbose:
            # Stream raw output straight through; nothing to capture or dump.
            self.note(label, "running")
            returncode = subprocess.run(argv, cwd=cwd, env=env).returncode
            return self._finish_step(label, detail, start, returncode, "")

        with self._status(label):
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        return self._finish_step(label, detail, start, proc.returncode, proc.stdout or "")

    def _finish_step(self, label: str, detail: str, start: float, returncode: int, log: str) -> str:
        """The single definition of how a step resolves: green ✓, or a red ✗ +
        framed log + ``StepError``. Shared by every step path so the failure
        contract lives in exactly one place."""
        if returncode != 0:
            self.fail(label)
            self._dump_log(log)
            raise StepError(label, returncode, log)
        self.ok(label, detail, timing=format_elapsed(time.monotonic() - start))
        return log

    def _dump_log(self, log: str) -> None:
        log = log.strip()
        if not log:
            return
        self.console.print(Text("      output", style="wt.muted"))
        for line in log.splitlines():
            self.console.print(Text("      " + line, style="wt.muted"))

    # --- data views (list / doctor) --------------------------------------------
    # These render a command's *data* (not progress), so they write to the
    # stdout data console and don't gate on ``quiet`` — --json callers emit
    # their own payload and never call them.

    def render_list(self, rows: list[WorktreeRow], repo_path: str) -> None:
        """Render the worktree list as a clean, aligned, color-coded view.

        Recognition lives here, not in directory names: NAME is the permanent
        identity, BRANCH is read live (the agent renames it as work takes
        shape), and LAST COMMIT + AGE carry the "which worktree was doing
        what" load. A placeholder (``treebox/*``) branch is always
        ``treebox/<name>``, so repeating it would be noise: the cell renders
        just ``⚠ unnamed`` so unpushable work is visible at a glance.
        """
        console = self.data_console
        console.print()
        if not rows:
            console.print(
                Text("  no worktrees yet — create one with ", style="wt.muted")
                + Text("treebox create", style="wt.accent")
            )
            console.print()
            return

        deps_style = {"fresh": "wt.fresh", "stale": "wt.stale", "unknown": "wt.unknown"}
        now = time.time()
        branches = ["⚠ unnamed" if r["unnamed"] else r["branch"] for r in rows]
        ages = [format_age(now - r["commit_epoch"]) if r["commit_epoch"] else "-" for r in rows]

        compact = len(rows) == 1
        table = Table(
            box=box.SIMPLE_HEAD,
            header_style="wt.muted",
            border_style="wt.muted",
            show_edge=False,
            pad_edge=False,
            padding=(0, 3, 0, 0),
            expand=False,
        )
        table.add_column("NAME", style="wt.name", no_wrap=True)
        table.add_column("BRANCH", no_wrap=True)
        table.add_column("LAST COMMIT", style="wt.muted", no_wrap=True)
        table.add_column("AGE", style="wt.muted", no_wrap=True, justify="right")
        table.add_column("DEPS", no_wrap=True)
        table.add_column("ENV", no_wrap=True)

        # A narrow terminal must cost the commit subject, never the identity or
        # status columns — Rich shrinks all no_wrap columns proportionally, so
        # truncate the subject ourselves to whatever the fixed columns leave.
        pad, indent, slack = 3, 2, 2
        fixed = (
            max(len("NAME"), *(len(r["name"]) for r in rows)),
            max(len("BRANCH"), *(len(b) for b in branches)),
            max(len("AGE"), *(len(a) for a in ages)),
            max(len("DEPS"), *(len(r["deps"]) + 2 for r in rows)),
            max(len("ENV"), *(len(r["env"]) + 2 for r in rows)),
        )
        commit_width = console.width - indent - sum(fixed) - pad * 6 - slack
        commit_width = max(min(commit_width, 40), 10)

        for r, branch, age in zip(rows, branches, ages, strict=True):
            branch_cell = Text(branch, style="wt.warn" if r["unnamed"] else "wt.name")
            deps = r["deps"]
            deps_cell = Text(f"● {deps}", style=deps_style[deps])
            env_ok = r["env"] == "present"
            env_cell = Text(
                ("● " if env_ok else "○ ") + r["env"],
                style="wt.fresh" if env_ok else "wt.unknown",
            )
            subject = r["last_commit"] or "-"
            if len(subject) > commit_width:
                subject = subject[: commit_width - 1].rstrip() + "…"
            table.add_row(r["name"], branch_cell, subject, age, deps_cell, env_cell)

        console.print(Padding(table, (0, 0, 0, 2)))
        if compact:
            console.print()
            return

        stale = sum(1 for r in rows if r["deps"] == "stale")
        unnamed = sum(1 for r in rows if r["unnamed"])
        caption = f"{len(rows)} worktree{'s' if len(rows) != 1 else ''}"
        if unnamed:
            caption += f" · {unnamed} unnamed"
        if stale:
            caption += f" · {stale} stale"
        console.print()
        console.print(Text("  Summary: " + caption, style="wt.muted"))
        if unnamed:
            console.print(
                Text(
                    "  Rename unnamed: git branch -m <type>/<short-name>",
                    style="wt.muted",
                )
            )
        console.print()

    def render_template_list(
        self,
        rows: list[TemplateRow],
        *,
        highlights: Sequence[str] = (),
    ) -> None:
        """Render the sandbox templates in the same visual language as ``list``:
        an aligned, color-coded NAME · SOURCE · STATUS table with the default
        flagged, plus — when ``default`` still resolves to the bundled image —
        a short highlight of the main blocks that image ships.
        """
        console = self.data_console
        console.print()
        if not rows:
            console.print(Text("  no templates", style="wt.muted"))
            console.print()
            return

        table = Table(
            box=box.SIMPLE_HEAD,
            header_style="wt.muted",
            border_style="wt.muted",
            show_edge=False,
            pad_edge=False,
            padding=(0, 3, 0, 0),
            expand=False,
        )
        table.add_column("NAME", style="wt.name", no_wrap=True)
        table.add_column("SOURCE", style="wt.muted", no_wrap=True)
        table.add_column("STATUS", no_wrap=True)

        # A narrow terminal must cost the (rare, edge-case) missing-files detail,
        # never the identity or source columns — Rich would otherwise shrink all
        # no_wrap columns proportionally and truncate NAME. So budget the fixed
        # columns and clip the STATUS text ourselves to whatever's left.
        suffix = "  (default)"
        pad, indent, slack, cols = 3, 2, 2, 3
        name_w = max(
            len("NAME"), *(len(r["name"]) + (len(suffix) if r["default"] else 0) for r in rows)
        )
        source_w = max(len("SOURCE"), *(len(r["source"]) for r in rows))
        status_w = console.width - indent - name_w - source_w - pad * cols - slack
        status_w = max(status_w, 12)

        for r in rows:
            name_cell = Text(r["name"], style="wt.name")
            if r["default"]:
                name_cell.append(suffix, style="wt.accent")
            if r["valid"]:
                status_cell = Text("● ok", style="wt.fresh")
            else:
                detail = "○ missing: " + ", ".join(r["missing"])
                if len(detail) > status_w:
                    detail = detail[: status_w - 1].rstrip() + "…"
                status_cell = Text(detail, style="wt.fail")
            table.add_row(name_cell, r["source"], status_cell)

        console.print(Padding(table, (0, 0, 0, 2)))

        if highlights:
            console.print()
            console.print(Text("  Bundled 'default' template includes", style="wt.muted"))
            for item in highlights:
                console.print(Text("    ● ", style="wt.fresh") + Text(item, style="wt.detail"))

        console.print()
        caption = f"{len(rows)} template{'s' if len(rows) != 1 else ''}"
        console.print(Text("  Summary: " + caption, style="wt.muted"))
        console.print()

    def _doctor_row(self, name: str, ok: bool, detail: str, width: int) -> None:
        """One aligned, color-coded doctor checklist row."""
        row = Text("    ")
        row.append("✓ " if ok else "✗ ", style="wt.ok" if ok else "wt.fail")
        row.append(f"{name:<{width}}", style="wt.name" if ok else "wt.fail")
        row.append("   ")
        row.append(detail, style="wt.muted" if ok else "wt.fail")
        self.data_console.print(row)

    def render_doctor(
        self,
        cheap: list[DoctorCheck],
        slow: Sequence[tuple[str, Callable[[], DoctorCheck]]],
        isolation: str,
        width: int,
    ) -> tuple[list[DoctorCheck], list[str]]:
        """Render the doctor checklist live: instant rows print at once; each slow
        check shows a dim spinner that resolves into a ✓/✗ row — no silent pause
        while the network / Docker daemon is probed. Returns the full check list +
        advisories so the caller can render the verdict and pick an exit code."""
        console = self.data_console
        console.print()
        console.print(
            Text("  ● ", style="wt.accent")
            + Text("doctor", style="wt.label")
            + Text(f"   isolation: {isolation}", style="wt.muted")
        )
        console.print()

        checks = list(cheap)
        for c in cheap:
            self._doctor_row(c.name, c.ok, c.detail, width)

        advisories: list[str] = []
        for label, check in slow:
            spinner = (
                console.status(
                    Text("    " + label, style="wt.muted"),
                    spinner="dots",
                    spinner_style="wt.accent",
                )
                if console.is_terminal
                else nullcontext()
            )
            with spinner:
                result = check()
            self._doctor_row(result.name, result.ok, result.detail, width)
            checks.append(result)
            if result.advisory:
                advisories.append(result.advisory)
        return checks, advisories

    def render_doctor_verdict(
        self,
        *,
        problems: list[str],
        has_login: bool,
        advisories: list[str] | None = None,
    ) -> None:
        """Render the advisory block and closing verdict line under the checklist."""
        console = self.data_console
        for advisory in advisories or []:
            console.print()
            console.print(Text("  ! ", style="wt.warn") + Text(advisory, style="wt.warn"))

        console.print()
        if problems:
            console.print(
                Text("  ✗ ", style="wt.fail")
                + Text("blocked: ", style="wt.fail")
                + Text(", ".join(problems), style="wt.detail")
            )
        elif advisories:
            # The advisory lines above are the takeaway; don't claim all-clear.
            console.print(
                Text("  ! ", style="wt.warn")
                + Text("usable, but see the advisory above", style="wt.warn")
            )
        elif not has_login:
            # Derived from the harness registry: every login dir listed, and
            # the default (first) harness's CLI named as the way to log in.
            line = Text("  ! ", style="wt.warn")
            line.append("no subscription login in ", style="wt.warn")
            for i, harness in enumerate(HARNESSES):
                if i:
                    line.append(" / ", style="wt.warn")
                line.append(harness.login_dir(), style="wt.accent")
            line.append(" — run ", style="wt.warn")
            line.append(HARNESSES[0].name, style="wt.accent")
            line.append(" once to log in", style="wt.warn")
            console.print(line)
        else:
            console.print(
                Text("  ✓ ", style="wt.ok") + Text("everything looks good", style="wt.ok")
            )
        console.print()
