"""The e2e run's docker event watch survives a subscription that goes live late.

``docker events`` connects to the engine some time after it starts, and the
engine never delivers an event from before that. On a busy CI runner the
watch's first marker volume was made in that gap, its event never came, and
X5 failed the whole run ("no docker event for treebox-e2e-sync-…-1 in 30s").
A fake ``docker`` reproduces the gap without an engine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

import harness  # noqa: E402

# `docker volume create NAME` appends NAME to a queue. `docker events` goes
# live after FAKE_DOCKER_LIVE_TENTHS tenths of a second (in short sleeps, so
# a terminate stops it at once) and then prints a volume create event for
# each NAME queued after that moment, never for an earlier one.
FAKE_DOCKER = r"""#!/bin/sh
q="$FAKE_DOCKER_DIR/created"
case "$1" in
  events)
    i=0
    while [ "$i" -lt "$FAKE_DOCKER_LIVE_TENTHS" ]; do sleep 0.1; i=$((i + 1)); done
    seen=$(wc -l < "$q")
    while :; do
      n=$(wc -l < "$q")
      if [ "$n" -gt "$seen" ]; then
        sed -n "$((seen + 1)),${n}p" "$q" | while read -r v; do
          printf '{"Type":"volume","Action":"create","Actor":{"ID":"%s"}}\n' "$v"
        done
        seen=$n
      fi
      sleep 0.1
    done
    ;;
  volume)
    if [ "$2" = create ]; then echo "$3" >> "$q"; fi
    ;;
esac
"""


@pytest.fixture
def late_engine(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    (tmp_path / "created").write_text("")
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_DIR": str(tmp_path),
        "FAKE_DOCKER_LIVE_TENTHS": "15",
    }


def test_the_watch_waits_until_the_subscription_is_live(
    late_engine: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness.VolumeWatch, "SYNC_TIMEOUT", 8.0)
    watch = harness.VolumeWatch(late_engine)
    try:
        assert watch.error is None
        watch.sync()  # a later sync needs one marker: the stream is live
        assert watch.error is None
    finally:
        watch.close()


def test_a_stream_that_never_goes_live_fails_the_watch(
    late_engine: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness.VolumeWatch, "SYNC_TIMEOUT", 2.0)
    watch = harness.VolumeWatch(dict(late_engine, FAKE_DOCKER_LIVE_TENTHS="600"))
    try:
        assert watch.error == "no docker event for a sync marker in 2s"
    finally:
        watch.close()
