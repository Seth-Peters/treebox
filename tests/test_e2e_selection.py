"""Select scenarios through the evidence command and read their durable results."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("selection", "expected_ids"),
    [
        ("F4", ["F4"]),
        ("B6,B7", ["B6", "B7"]),
        ("F3,F4", ["F3", "F4"]),
        ("E3", ["E3"]),
        ("R45b", ["R45b"]),
    ],
)
def test_selected_scenarios_own_their_state(
    tmp_path: Path, selection: str, expected_ids: list[str]
) -> None:
    if selection == "R45b" and os.geteuid() == 0:
        pytest.skip("the unreadable-file scenario needs a non-root user")
    tools = tmp_path / "tools"
    tools.mkdir()
    # Host-only evidence must not probe, pull images from, or reap a real engine.
    docker = tools / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n")
    docker.chmod(0o755)
    env = dict(os.environ, PATH=os.pathsep.join([str(tools), os.environ["PATH"]]))
    out = tmp_path / "evidence"
    run = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "e2e" / "run.py"),
            "evidence",
            "--only",
            selection,
            "--out",
            str(out),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["exit_status"] == 0
    assert manifest["prerequisites"]["docker"]["available"] is False
    results = json.loads((out / "results.json").read_text())["results"]
    assert [(r["id"], r["verdict"]) for r in results] == [(sid, "pass") for sid in expected_ids]
    for result in results:
        assert result["runs"], result
        assert (out / result["state"]).is_file()
        for record in result["runs"]:
            assert (out / record["stdout"]).is_file()
            assert (out / record["stderr"]).is_file()
    setups = manifest["group_setups"]
    assert [(entry["id"], entry["ok"]) for entry in setups] == [
        (f"{sid}-setup", True) for sid in expected_ids
    ]
