# The bundled sandbox image

treebox ships a Dockerfile, not an image. The three decisions here explain
why the first `create` is slow on purpose, why the Dockerfile avoids newer
build syntax, and why a browser in the box runs with its own sandbox off.

## Decisions

### The image is never published; everyone builds it locally from pinned inputs

A public prebuilt image (for example on `ghcr.io`) would make the first
`create` fast. It was rejected: everyone who pulls it has to trust whoever
built it, for an image that holds an agent with credentials. Instead the
template builds locally and reproducibly: the base image is pinned by index
digest, every download by version plus a per-architecture SHA-256, and there
are no `curl | bash` installers. Debian packages deliberately float: pinning
them through a snapshot service would freeze security fixes and depend on a
slow service. Tools were removed from the image only with a functional proof
(a real build, then Playwright and Python used inside it), and there is no
`full` variant: an operator restores tools in their own template.

Decided: 12 November 2026, issues #70 and #72, PRs #75 and #81.
Where: `src/treebox/assets/container/Dockerfile`, `treebox-fetch`,
`treebox-online`

### The Dockerfile builds with Docker's legacy builder

No BuildKit-only construct is allowed in the bundled Dockerfile. The first
reproducible version used heredoc `COPY --chmod`, which only BuildKit
understands, and it failed on a Linux host without buildx. Requiring
BuildKit was rejected: it would add a hard `doctor` check, a new classified
error and an install step on those hosts, for no gain in the image. The
build helpers are therefore plain files the Dockerfile copies, and the image
test builds with both builders.

Decided: PR #75.
Where: `src/treebox/assets/container/Dockerfile`;
`tests/test_sandbox_image.py`

### Chromium runs without its own sandbox, set in the image

Chromium cannot start its sandbox under Docker's default seccomp profile.
The alternatives all weaken the container for every process in it:
`seccomp=unconfined`, `--privileged`, added capabilities, or a host sysctl.
The container is the isolation boundary and the agent in it already runs any
code it likes, so Chromium's sandbox adds no protection against the agent.
The switch is an image `ENV`, not a `container.json` value, so a target repo
cannot turn it back on; an operator does that in their template.

Decided: 13 November 2026, PR #89.
Where: `src/treebox/assets/container/Dockerfile` · `PLAYWRIGHT_MCP_SANDBOX`
