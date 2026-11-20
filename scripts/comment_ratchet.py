#!/usr/bin/env python3
"""The comment ratchet: no tracked Python file may gain a ``#`` comment.

``scripts/comment-baseline.json`` holds, per file, how many comments it still
has. A file absent from the baseline allows none. The check fails when a file
has more comments than its baseline (delete the comment) and when it has fewer
(run ``--update`` so the lower count becomes the new limit). ``--update`` only
ever lowers a count.

Directive comments (``noqa``, ``type: ignore``, ``pragma:``, ``fmt:``,
``ruff:``, ``mypy:``, ``pyright:``), a shebang and an encoding cookie are not
counted. Docstrings are not comments.

    scripts/comment_ratchet.py            check
    scripts/comment_ratchet.py --update   lower the baseline to today's counts
    scripts/comment_ratchet.py --list F   print the comments of file F
    scripts/comment_ratchet.py --strip F  delete the comments of file F
"""

from __future__ import annotations

import argparse
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
DIRECTIVE = re.compile(r"#\s*(noqa|type:\s*ignore|pragma:|fmt:|ruff:|mypy:|pyright:)")
ENCODING = re.compile(r"#.*coding[:=]")
ADVICE = (
    "Say it in the code instead: a name, a type, a test, or a docstring that "
    "states a contract. A non-obvious decision goes in docs/adr/. "
    "The delete-comment skill (.claude/skills/delete-comment) does the whole pass."
)


@dataclass(frozen=True)
class Comment:
    line: int
    column: int
    text: str
    own_line: bool


def comments(source: str) -> list[Comment]:
    lines = source.splitlines()
    found = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        line, column = token.start
        if line == 1 and token.string.startswith("#!"):
            continue
        if line <= 2 and ENCODING.match(token.string):
            continue
        if DIRECTIVE.match(token.string):
            continue
        own_line = lines[line - 1].lstrip().startswith("#")
        found.append(Comment(line, column, token.string, own_line))
    return found


def strip(source: str) -> str:
    lines = source.splitlines(keepends=True)
    for comment in reversed(comments(source)):
        index = comment.line - 1
        if comment.own_line:
            del lines[index]
            continue
        ending = "\n" if lines[index].endswith("\n") else ""
        lines[index] = lines[index][: comment.column].rstrip() + ending
    return "".join(lines)


def tracked_python_files(root: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(name for name in listed.split("\0") if name and (root / name).is_file())


def count_all(root: Path) -> dict[str, int]:
    counts = {}
    for name in tracked_python_files(root):
        number = len(comments((root / name).read_text(encoding="utf-8")))
        if number:
            counts[name] = number
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


def report_grown(root: Path, over: dict[str, tuple[int, int]]) -> None:
    for name, (allowed, number) in over.items():
        print(f"{name}: {number} comments, the baseline allows {allowed}", file=sys.stderr)
        if allowed == 0:
            for comment in comments((root / name).read_text(encoding="utf-8")):
                print(f"  {name}:{comment.line}: {comment.text}", file=sys.stderr)
    print(f"\nComments are banned in Python files. {ADVICE}", file=sys.stderr)


def check(root: Path, baseline_path: Path) -> int:
    counts, baseline = count_all(root), load_baseline(baseline_path)
    over, under = grown(counts, baseline), shrunk(counts, baseline)
    if over:
        report_grown(root, over)
        return 1
    if under:
        for name, (allowed, number) in under.items():
            print(
                f"{name}: {number} comments, the baseline still allows {allowed}", file=sys.stderr
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
    print(f"{sum(counts.values())} comments left in {len(counts)} files")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="lower the baseline")
    mode.add_argument("--init", action="store_true", help="write a first baseline (may raise)")
    mode.add_argument("--list", nargs="+", metavar="FILE", help="print the comments of FILE")
    mode.add_argument("--strip", nargs="+", metavar="FILE", help="delete the comments of FILE")
    args = parser.parse_args(argv)
    if args.list:
        for name in args.list:
            for comment in comments(Path(name).read_text(encoding="utf-8")):
                print(f"{name}:{comment.line}: {comment.text}")
        return 0
    if args.strip:
        for name in args.strip:
            path = Path(name)
            path.write_text(strip(path.read_text(encoding="utf-8")), encoding="utf-8")
        return 0
    if args.update or args.init:
        return update(ROOT, BASELINE, init=args.init)
    return check(ROOT, BASELINE)


if __name__ == "__main__":
    sys.exit(main())
