"""Opt-in Docker builds and runtime checks for the bundled sandbox.

Run with TREEBOX_TEST_SANDBOX_IMAGE=1 and pytest -s tests/test_sandbox_image.py.
Both builders run on the daemon's native platform by default. Set
TREEBOX_TEST_SANDBOX_PLATFORM to linux/amd64 or linux/arm64 to select a platform.
Use -k legacy or -k buildkit to select a builder. Docker, the selected builder,
registry access, and native execution or emulation of that platform are required.
An enabled run fails if a prerequisite is missing. Builds can use Docker's cache.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox.cli import app
from treebox.config import Config
from treebox.models import Worktree
from treebox.runners.docker import DockerRunner


def _run(argv: list[str], **kwargs) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120, **kwargs)
    assert result.returncode == 0, f"{argv!r}\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


@dataclass(frozen=True)
class SandboxImage:
    tag: str
    platform: str

    def run(self, *argv: str, input: str | None = None, user: str = "agent") -> str:
        return _run(
            [
                "docker",
                "run",
                "--rm",
                "--init",
                "--network=none",
                "--platform",
                self.platform,
                "--user",
                user,
                "-i",
                self.tag,
                *argv,
            ],
            input=input,
        )


@pytest.fixture(scope="module", params=["legacy", "buildkit"])
def sandbox_image(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[SandboxImage]:
    if os.environ.get("TREEBOX_TEST_SANDBOX_IMAGE") != "1":
        pytest.skip("set TREEBOX_TEST_SANDBOX_IMAGE=1 to build and run the sandbox with Docker")
    assert shutil.which("docker"), "Docker is required for an enabled sandbox image test"
    info = json.loads(_run(["docker", "info", "--format", "{{json .}}"]))
    architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(
        info["Architecture"], info["Architecture"]
    )
    platform = os.environ.get("TREEBOX_TEST_SANDBOX_PLATFORM", f"linux/{architecture}")
    assert platform in {"linux/amd64", "linux/arm64"}, platform
    root = tmp_path_factory.mktemp(f"sandbox-{request.param}")
    repo = root / "repo"
    _run(["git", "init", "-q", str(repo)])
    wt = Worktree(str(repo), "image-test", "image-test", "main", root / "image-test")
    runner = DockerRunner(Config(isolation="docker"))
    with pytest.MonkeyPatch.context() as env:
        env.setenv("TREEBOX_HOME", str(root / "treebox-home"))
        env.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
        config = runner._write_config(wt, cold=True)
    argv = runner._build_command(wt, config)
    argv[2:2] = ["--platform", platform, "--force-rm"]
    tag = argv[argv.index("-t") + 1]
    build_env = dict(os.environ, DOCKER_BUILDKIT="0" if request.param == "legacy" else "1")
    log = root / "build.log"
    print(
        f"\nFresh build invocation: engine={info['ServerVersion']} driver={info['Driver']} "
        f"builder={request.param} platform={platform} cache=enabled log={log}",
        flush=True,
    )
    built = False
    try:
        with log.open("w") as output:
            result = subprocess.run(
                argv,
                env=build_env,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1800,
            )
        built = result.returncode == 0
        assert built, f"Build failed; full log: {log}\n{log.read_text()[-8000:]}"
        image = json.loads(_run(["docker", "image", "inspect", tag]))[0]
        assert f"{image['Os']}/{image['Architecture']}" == platform
        yield SandboxImage(tag, platform)
    finally:
        if built:
            _run(["docker", "image", "rm", tag])


@pytest.mark.parametrize(
    ("argv", "version"),
    [
        (("python", "--version"), "3.14.6"),
        (("node", "--version"), "22.23.5"),
        (("gh", "--version"), "2.104.0"),
        (("uv", "--version"), "0.13.8"),
        (("claude", "--version"), "2.1.311"),
        (("codex", "--version"), "0.167.2"),
        (("playwright", "--version"), "1.65.0"),
    ],
)
def test_installed_tool_versions(sandbox_image: SandboxImage, argv: tuple[str, ...], version: str):
    output = sandbox_image.run(*argv)
    actual = re.search(r"(?<!\d)\d+\.\d+\.\d+(?!\d)", output)
    assert actual is not None, output
    assert actual.group() == version


def test_installed_npm_package_versions(sandbox_image: SandboxImage):
    packages = json.loads(sandbox_image.run("npm", "ls", "-g", "--depth=0", "--json"))
    versions = {name: value["version"] for name, value in packages["dependencies"].items()}
    assert {
        name: versions[name] for name in ("@playwright/cli", "playwright", "@openai/codex")
    } == {
        "@playwright/cli": "0.1.24",
        "playwright": "1.65.0",
        "@openai/codex": "0.167.2",
    }


def test_image_has_headless_shell_and_no_download_caches(sandbox_image: SandboxImage):
    state = json.loads(
        sandbox_image.run(
            "python",
            "-",
            user="root",
            input="""
import json
import os
from pathlib import Path

browsers = Path('/ms-playwright')
shells = list(browsers.rglob('chrome-headless-shell'))
caches = [Path('/root/.npm/_cacache'), Path('/home/agent/.npm/_cacache'),
          Path('/var/lib/apt/lists'), Path('/var/cache/apt/archives')]
print(json.dumps({
    'browsers': sorted(p.name.rsplit('-', 1)[0] for p in browsers.iterdir()
                       if not p.name.startswith('.')),
    'shells': len(shells),
    'chrome_target': len(shells) == 1 and
                     Path('/opt/google/chrome/chrome').resolve() == shells[0].resolve(),
    'shell_executable': len(shells) == 1 and os.access(shells[0], os.X_OK),
    'cached_files': [str(p) for cache in caches for p in cache.rglob('*')
                     if p.is_file() and p.name != 'lock'],
}))
""",
        )
    )
    assert state == {
        "browsers": ["chromium_headless_shell", "ffmpeg"],
        "shells": 1,
        "chrome_target": True,
        "shell_executable": True,
        "cached_files": [],
    }


def test_playwright_library_launches_and_renders(sandbox_image: SandboxImage):
    output = sandbox_image.run(
        "sh",
        "-c",
        'export NODE_PATH="$(npm root -g)"; exec node',
        input="""
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent('<title>treebox browser probe</title><h1>Rendered</h1>');
    console.log(JSON.stringify({
      title: await page.title(),
      heading: await page.locator('h1').innerText(),
      png: (await page.screenshot()).subarray(0, 8).toString('hex'),
    }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
""",
    )
    assert json.loads(output) == {
        "title": "treebox browser probe",
        "heading": "Rendered",
        "png": "89504e470d0a1a0a",
    }


def test_playwright_cli_launches_and_evaluates(sandbox_image: SandboxImage):
    output = sandbox_image.run(
        "sh",
        "-ec",
        "cd /tmp; playwright-cli open about:blank; "
        "trap 'playwright-cli close' EXIT; "
        "playwright-cli eval '() => 6 * 7'",
    )
    assert re.search(r"(?m)^42$", output), output


def test_download_helper_checks_bytes(sandbox_image: SandboxImage):
    output = sandbox_image.run(
        "python",
        "-",
        input="""
import functools
import hashlib
import http.server
import subprocess
import tempfile
import threading
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / 'payload').write_bytes(b'treebox checksum probe\\n')
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/payload'
            sha = hashlib.sha256(b'treebox checksum probe\\n').hexdigest()
            good = subprocess.run(['treebox-fetch', url, sha, str(root / 'good')],
                                  capture_output=True, text=True)
            assert good.returncode == 0, good.stderr
            assert (root / 'good').read_bytes() == b'treebox checksum probe\\n'
            bad = subprocess.run(['treebox-fetch', url, '0' * 64, str(root / 'bad')],
                                 capture_output=True, text=True)
            assert bad.returncode != 0
            assert 'SHA-256 mismatch' in bad.stderr, bad.stderr
        finally:
            server.shutdown()
            thread.join()
print('checksum accepted; incorrect checksum rejected')
""",
    )
    assert output == "checksum accepted; incorrect checksum rejected"


def test_template_list_highlights_match_installed_versions(
    sandbox_image: SandboxImage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TREEBOX_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
    config = tmp_path / "config.toml"
    config.write_text("")
    monkeypatch.setenv("TREEBOX_CONFIG", str(config))
    python = sandbox_image.run("python", "-c", "import sys; print('%s.%s' % sys.version_info[:2])")
    node = sandbox_image.run("node", "-p", "process.versions.node.split('.')[0]")
    result = CliRunner().invoke(app, ["template", "list"])
    assert result.exit_code == 0, result.output
    assert f"Python {python} + uv" in result.output
    assert f"Node.js {node} + npm" in result.output
