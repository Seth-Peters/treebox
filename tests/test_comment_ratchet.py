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


YAML = """# a header
name: CI  # trailing
url: "http://x/#anchor"
steps:
  - uses: actions/checkout@3d3c42e # v7.0.1
  - run: |
      # shell, not yaml
      echo "$#"
  - name: after  # counted again
"""

DOCSTRINGS = '''"""Module."""


def run():
    """One line."""


class Box:
    """Two
    lines."""

    def get(self):
        return "not a docstring"
'''


def test_only_prose_comments_count(ratchet: ModuleType) -> None:
    found = ratchet.python_comments(SOURCE)
    assert [(c.line, c.text, c.own_line) for c in found] == [
        (3, "# a banner", True),
        (7, "# why one", False),
    ]


def test_docstrings_count_by_the_lines_they_span(ratchet: ModuleType) -> None:
    found = ratchet.comments("m.py", DOCSTRINGS)
    assert [(c.line, c.lines, c.docstring) for c in found] == [
        (1, 1, True),
        (5, 1, True),
        (9, 2, True),
    ]


def test_yaml_counts_comments_but_not_pins_or_embedded_scripts(ratchet: ModuleType) -> None:
    found = ratchet.comments("ci.yml", YAML)
    assert [(c.line, c.text, c.own_line) for c in found] == [
        (1, "# a header", True),
        (2, "# trailing", False),
        (9, "# counted again", False),
    ]
    stripped = ratchet.strip("ci.yml", YAML)
    assert ratchet.comments("ci.yml", stripped) == []
    assert "name: CI\n" in stripped
    assert "# v7.0.1" in stripped
    assert "# shell, not yaml" in stripped
    assert "#anchor" in stripped


def test_strip_deletes_comments_and_keeps_directives(ratchet: ModuleType) -> None:
    stripped = ratchet.strip("m.py", SOURCE)
    assert ratchet.python_comments(stripped) == []
    assert '"""A docstring with a # in it."""' in stripped
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
    _track(repo, "new.yml", "# fresh\na: 1\n")
    assert ratchet.check(repo, baseline) == 1
    err = capsys.readouterr().err
    assert "new.py:1: comment: # fresh" in err
    assert "new.yml:1: comment: # fresh" in err


def test_a_longer_docstring_fails_the_check(ratchet: ModuleType, repo: Path) -> None:
    baseline = repo / "scripts" / "comment-baseline.json"
    _track(repo, "old.py", 'def run():\n    """One."""\n')
    ratchet.update(repo, baseline, init=True)
    assert json.loads(baseline.read_text()) == {"old.py": 1}

    _track(repo, "old.py", 'def run():\n    """One.\n\n    And why."""\n')
    assert ratchet.check(repo, baseline) == 1


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
