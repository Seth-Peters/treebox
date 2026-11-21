#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "comment-baseline.json"
PATTERNS = ("*.py", "*.yml", "*.yaml")
DESCRIPTION = (
    "The comment ratchet: no tracked Python or YAML file may gain comment lines. "
    "A Python '#' comment counts as one line, a docstring as the lines it spans, "
    "a YAML '#' comment as one line. scripts/comment-baseline.json holds the lines "
    "each file still has; a file not listed there allows none. The check fails when "
    "a file has more lines than its baseline, and when it has fewer until --update "
    "lowers the baseline. Not counted: noqa / type: ignore / pragma / fmt / ruff / "
    "mypy / pyright directives, a shebang, an encoding cookie, the version pin after "
    "a YAML 'uses:', and '#' inside a YAML block scalar (that is the embedded script)."
)
PYTHON_DIRECTIVE = re.compile(r"#\s*(noqa|type:\s*ignore|pragma:|fmt:|ruff:|mypy:|pyright:)")
ENCODING = re.compile(r"#.*coding[:=]")
YAML_VERSION_PIN = re.compile(r"\buses:\s*\S+\s+#\s*v?\d[\w.-]*\s*$")
YAML_BLOCK_SCALAR = re.compile(r"(^|[\s:-])[|>][+-]?\d?[+-]?\s*$")
ADVICE = (
    "Say it in the code instead: a name, a type, or a test. A non-obvious decision "
    "goes in docs/adr/. A Typer command's docstring is its --help text: pass help= "
    "instead. The delete-comment skill (.claude/skills/delete-comment) does the "
    "whole pass."
)


@dataclass(frozen=True)
class Comment:
    line: int
    column: int
    text: str
    own_line: bool
    lines: int = 1
    docstring: bool = False


def python_comments(source: str) -> list[Comment]:
    rows = source.splitlines()
    found = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        line, column = token.start
        if line == 1 and token.string.startswith("#!"):
            continue
        if line <= 2 and ENCODING.match(token.string):
            continue
        if PYTHON_DIRECTIVE.match(token.string):
            continue
        own_line = rows[line - 1].lstrip().startswith("#")
        found.append(Comment(line, column, token.string, own_line))
    return found


def docstrings(source: str) -> list[Comment]:
    found = []
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        span = (first.end_lineno or first.lineno) - first.lineno + 1
        summary = first.value.value.strip().splitlines()[0] if first.value.value.strip() else ""
        found.append(Comment(first.lineno, first.col_offset, f'"""{summary}', True, span, True))
    return found


def _yaml_comment_column(row: str) -> int | None:
    quote = ""
    index = 0
    while index < len(row):
        char = row[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or row[index - 1] in " \t"):
            return index
        index += 1
    return None


def yaml_comments(source: str) -> list[Comment]:
    found = []
    scalar_indent: int | None = None
    for number, row in enumerate(source.splitlines(), 1):
        indent = len(row) - len(row.lstrip(" "))
        if scalar_indent is not None:
            if not row.strip() or indent > scalar_indent:
                continue
            scalar_indent = None
        column = _yaml_comment_column(row)
        code = row if column is None else row[:column]
        if YAML_BLOCK_SCALAR.search(code.rstrip()):
            scalar_indent = indent
        if column is None or YAML_VERSION_PIN.search(row):
            continue
        found.append(Comment(number, column, row[column:].rstrip(), not code.strip()))
    return found


def comments(name: str, source: str) -> list[Comment]:
    if name.endswith(".py"):
        return sorted(python_comments(source) + docstrings(source), key=lambda c: c.line)
    return yaml_comments(source)


def strip(name: str, source: str) -> str:
    rows = source.splitlines(keepends=True)
    for comment in reversed(comments(name, source)):
        if comment.docstring:
            continue
        index = comment.line - 1
        if comment.own_line:
            del rows[index]
            continue
        ending = "\n" if rows[index].endswith("\n") else ""
        rows[index] = rows[index][: comment.column].rstrip() + ending
    return "".join(rows)


def tracked_files(root: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *PATTERNS],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(name for name in listed.split("\0") if name and (root / name).is_file())


def count_all(root: Path) -> dict[str, int]:
    counts = {}
    for name in tracked_files(root):
        found = comments(name, (root / name).read_text(encoding="utf-8"))
        total = sum(comment.lines for comment in found)
        if total:
            counts[name] = total
    return counts


def load_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    loaded: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def save_baseline(path: Path, counts: dict[str, int]) -> None:
    path.write_text(json.dumps(dict(sorted(counts.items())), indent=2) + "\n", encoding="utf-8")


def grown(counts: dict[str, int], baseline: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {
        name: (baseline.get(name, 0), number)
        for name, number in sorted(counts.items())
        if number > baseline.get(name, 0)
    }


def shrunk(counts: dict[str, int], baseline: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {
        name: (allowed, counts.get(name, 0))
        for name, allowed in sorted(baseline.items())
        if counts.get(name, 0) < allowed
    }


def describe(name: str, comment: Comment) -> str:
    kind = f"docstring, {comment.lines} lines" if comment.docstring else "comment"
    return f"{name}:{comment.line}: {kind}: {comment.text}"


def report_grown(root: Path, over: dict[str, tuple[int, int]]) -> None:
    for name, (allowed, number) in over.items():
        print(f"{name}: {number} comment lines, the baseline allows {allowed}", file=sys.stderr)
        if allowed == 0:
            for comment in comments(name, (root / name).read_text(encoding="utf-8")):
                print(f"  {describe(name, comment)}", file=sys.stderr)
    print(f"\nComments and docstrings are banned. {ADVICE}", file=sys.stderr)


def check(root: Path, baseline_path: Path) -> int:
    counts, baseline = count_all(root), load_baseline(baseline_path)
    over, under = grown(counts, baseline), shrunk(counts, baseline)
    if over:
        report_grown(root, over)
        return 1
    if under:
        for name, (allowed, number) in under.items():
            print(
                f"{name}: {number} comment lines, the baseline still allows {allowed}",
                file=sys.stderr,
            )
        print(
            "\nComments were deleted. Lock that in: scripts/comment_ratchet.py --update",
            file=sys.stderr,
        )
        return 1
    return 0


def update(root: Path, baseline_path: Path, *, init: bool) -> int:
    counts = count_all(root)
    if not init:
        over = grown(counts, load_baseline(baseline_path))
        if over:
            report_grown(root, over)
            print("\nThe baseline only goes down; nothing was written.", file=sys.stderr)
            return 1
    save_baseline(baseline_path, counts)
    print(f"{sum(counts.values())} comment lines left in {len(counts)} files")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="lower the baseline to today's counts")
    mode.add_argument("--init", action="store_true", help="write a first baseline (may raise)")
    mode.add_argument("--list", nargs="+", metavar="FILE", help="print what counts in FILE")
    mode.add_argument(
        "--strip",
        nargs="+",
        metavar="FILE",
        help="delete the '#' comments of FILE (docstrings are removed by hand)",
    )
    args = parser.parse_args(argv)
    if args.list:
        for name in args.list:
            for comment in comments(name, Path(name).read_text(encoding="utf-8")):
                print(describe(name, comment))
        return 0
    if args.strip:
        for name in args.strip:
            path = Path(name)
            path.write_text(strip(name, path.read_text(encoding="utf-8")), encoding="utf-8")
        return 0
    if args.update or args.init:
        return update(ROOT, BASELINE, init=args.init)
    return check(ROOT, BASELINE)


if __name__ == "__main__":
    sys.exit(main())
