# Terminal output and interactive rendering

treebox is read by people at a terminal and by scripts through pipes, often
in the same session. Output follows one quiet visual style, sends data to
stdout and everything diagnostic to stderr, and lets only a real terminal's
width shape what is printed, so redirected output is stable and narrow
terminals lose the least important detail first.

## Decisions

### One minimal style, with color reserved for status

The theme follows the uv/Astral minimal look: mostly dim and default text,
one colored glyph per line, and right-aligned dim timings. The only saturated
colors are the status glyph (green `✓`, red `✗`) and the freshness dots in
`list`. Color then always means status, never decoration.

Where: `output.py` · `THEME`

### Status rows share one column grid

Status rows are indented two columns so the glyph sits at column 2. The
spinner is padded to the same column, so a live spinner frame and the
resolved `✓` that replaces it line up exactly. The label column is padded to
14 characters for a clean left edge, and a right margin of 2 columns is kept
clear of the terminal edge for the timing column.

A label is padded to the column width and then always followed by two more
spaces before its detail. Without the extra spaces, a label exactly as wide
as the column would run into its detail.

Where: `output.py` · `_INDENT`, `_LABEL_W`, `_RIGHT_MARGIN`, `Reporter._row`

### Status rows are soft-wrapped

Status rows are printed with `soft_wrap=True`. Rich never hard-wraps them in
the middle of a line, because a hard wrap drops the indent on the
continuation line. Overflow is left to the terminal.

Where: `output.py` · `Reporter._print`

### A console that is not a terminal has no width

A pipe or a file has no width: a script reads it line by line, so a table
row or a path must never be wrapped or cut there. Only a terminal's width
shapes the output. Rich still needs a number, so such a console gets
`_UNBOUNDED_WIDTH` (100,000), which only turns Rich's wrapping and cropping
off. It is never a width to fill or to budget against. Every renderer
therefore obeys two rules:

- It fits its output (cuts, wraps, shortens, right-aligns) only to
  `fit_width(console)`, which is `None` for an unbounded console: there is
  nothing to fit.
- It prints a table, or any block Rich could stretch, through `print_block`,
  which renders the block at its content width and pads no line.

Where: `output.py` · `_UNBOUNDED_WIDTH`, `new_console`, `fit_width`,
`print_block`

### Data views write to stdout and ignore `quiet`

The `list` and `doctor` views render a command's data, not its progress.
They write to the stdout data console and do not check `quiet`. This is safe
because `--json` callers emit their own payload and never call these
renderers.

Where: `output.py` · `Reporter.render_list`, `Reporter.render_doctor` (the
data-view methods)

### `list` keeps every core field at any width

To decide whether the full `list` table fits, its natural width is measured
without the real terminal constraint (against a width of at least 1000). If
the natural width fits the terminal, Rich will not shrink any no-wrap core
field, so the table is printed as is.

At narrow widths no core field is dropped or truncated. Instead the mutable
branch is placed directly under its stable name in one identity column. That
layout is narrower than truncating either value would be.

Where: `output.py` · `Reporter.render_list` (`build_table`, `natural_width`)

### `list` sorts by most recent commit first

Rows are sorted by commit time, newest first. The worktree someone is
looking for is almost always the one they, or an agent, just committed in.

Where: `cli.py` · `_collect_rows`

### `template list` clips only the STATUS detail

On a narrow terminal the cost falls on the missing-files detail in STATUS,
which is rare and an edge case, never on the identity or source columns.
Left alone, Rich shrinks all no-wrap columns proportionally and truncates
NAME. So the renderer budgets the fixed columns itself and clips the STATUS
text to whatever is left. Off a terminal there is no budget and the detail
stays whole.

Where: `output.py` · `Reporter.render_template_list`

### Doctor rows are never wrapped by Rich

Doctor rows can contain absolute paths. They are printed with
`soft_wrap=True` so redirected output is byte-stable. Otherwise Rich would
insert newlines that depend on the width and continue the text without its
indent.

Where: `output.py` · `Reporter._doctor_row`

### Doctor defers its slow checks as thunks

The checks that reach the network or the Docker daemon are what made
`doctor` appear to hang. They are deferred as thunks that return a row plus
an optional advisory. The human path wraps each one in a spinner; `--json`
runs them inline.

The git auth check exists because `create` requires a fresh fetch. Doctor
validates up front that git can authenticate to origin, and it exercises the
same silent paths `create` uses: ssh-agent first, then the HTTPS fallback
(host CLI, credential helper, token).

Where: `cli.py` · `doctor` (`_check_git_auth`, `_check_runner`)

### The teardown picker is diagnostic UI

The interactive picker and its numbered fallback print to the stderr
console. The picker is diagnostic UI, not data, so stdout stays clean.

The picker's badges use the same green, yellow and red meaning as the rest
of the CLI. `_PT_STYLE` maps each Rich theme style (`wt.ok`, `wt.warn`,
`wt.fail`) to the matching prompt_toolkit style so the two libraries agree.

The choice list starts with a blank separator between the question line and
the first choice, so the list does not crowd the prompt. Separators cannot
be selected and are never returned.

Where: `cli.py` · `_PT_STYLE`, `_prompt_selection`, `_prompt_numbered`

### SIGPIPE is restored to the default

Python starts with SIGPIPE ignored, which is wrong for a CLI that is piped.
On POSIX the entry callback restores the default handler, so piping output
into `head` or `grep -m1` ends the process quietly (SIGPIPE, exit 141)
instead of printing "BrokenPipeError ignored" or a traceback at shutdown.
The call is guarded by `hasattr(signal, "SIGPIPE")` for platforms without
the signal.

Where: `cli.py` · `main`

### Unexpected crashes get secret-safe tracebacks

Handled errors already exit cleanly through `_die`. For a truly unexpected
crash, Rich's traceback handler is installed with `show_locals=False`, so no
local variable (a token, a credential path) is printed, with Typer's
framework frames suppressed and a width of 100.

Where: `cli.py` · `main`
