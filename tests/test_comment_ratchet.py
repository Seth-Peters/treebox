"""The comment ratchet (scripts/comment_ratchet.py): what counts as a comment,
what stripping leaves behind, and that the baseline only goes down."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "comment_ratchet.py"

SOURCE = '''#!/usr/bin/env python3
"""A docstring with a # in it."""
# a banner
import os  # noqa: F401

TEXT = "# not a comment"
value = 1  # why one
kept = open("f")  # type: ignore[arg-type]
'''


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("comment_ratchet", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    return tmp_path


def _track(repo: Path, name: str, source: str) -> None:
    (repo / name).write_text(source, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)


def test_only_prose_comments_count(ratchet: ModuleType) -> None:
    found = ratchet.comments(SOURCE)
    assert [(c.line, c.text, c.own_line) for c in found] == [
        (3, "# a banner", True),
        (7, "# why one", False),
    ]


def test_strip_deletes_comments_and_keeps_directives(ratchet: ModuleType) -> None:
    stripped = ratchet.strip(SOURCE)
    assert ratchet.comments(stripped) == []
    assert "# a banner" not in stripped
    assert "value = 1\n" in stripped
    assert "# noqa: F401" in stripped
    assert "# type: ignore[arg-type]" in stripped
    assert 'TEXT = "# not a comment"' in stripped
    assert stripped.startswith("#!/usr/bin/env python3\n")


def test_a_new_comment_fails_the_check(
    ratchet: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = repo / "scripts" / "comment-baseline.json"
    _track(repo, "old.py", "x = 1  # legacy\n")
    assert ratchet.update(repo, baseline, init=True) == 0
    assert json.loads(baseline.read_text()) == {"old.py": 1}
    assert ratchet.check(repo, baseline) == 0

    _track(repo, "new.py", "y = 2  # fresh\n")
    assert ratchet.check(repo, baseline) == 1
    assert "new.py:1: # fresh" in capsys.readouterr().err


def test_the_baseline_only_goes_down(ratchet: ModuleType, repo: Path) -> None:
    baseline = repo / "scripts" / "comment-baseline.json"
    _track(repo, "old.py", "x = 1  # one\ny = 2  # two\n")
    ratchet.update(repo, baseline, init=True)

    _track(repo, "old.py", "x = 1  # one\ny = 2\n")
    assert ratchet.check(repo, baseline) == 1
    assert ratchet.update(repo, baseline, init=False) == 0
    assert json.loads(baseline.read_text()) == {"old.py": 1}

    _track(repo, "old.py", "x = 1  # one\ny = 2  # two\n")
    assert ratchet.update(repo, baseline, init=False) == 1
    assert json.loads(baseline.read_text()) == {"old.py": 1}


def test_this_repository_passes(ratchet: ModuleType) -> None:
    assert ratchet.check(ratchet.ROOT, ratchet.BASELINE) == 0
