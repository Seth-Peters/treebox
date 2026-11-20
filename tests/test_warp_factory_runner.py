"""Regression tests for the optional Docker Engine Factory setup."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / ".warp-factory" / "runners" / "start-docker.sh"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def run_script(bin_dir: Path, log: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{bin_dir}:/bin")
    if log is not None:
        env["DOCKER_LOG"] = str(log)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_docker_install_failure_preserves_skip_behavior(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "docker", "#!/bin/sh\nexit 1\n")
    write_executable(bin_dir / "apt-get", "#!/bin/sh\nexit 1\n")

    result = run_script(bin_dir)

    assert result.returncode == 0
    assert "docker scenarios are SKIP" in result.stderr


def test_ready_engine_is_probed_with_a_read_only_bind_mount(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    write_executable(
        bin_dir / "docker",
        """#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_LOG"
case "$1" in
  info|pull) exit 0 ;;
  run) exit 42 ;;
esac
""",
    )
    write_executable(bin_dir / "dockerd", "#!/bin/sh\nexit 1\n")

    result = run_script(bin_dir, log)

    assert result.returncode == 0
    assert result.stderr == ""
    assert "Docker engine validated" in result.stdout
    run_command = next(line for line in log.read_text().splitlines() if line.startswith("run "))
    assert "type=bind" in run_command
    assert "readonly" in run_command
