# The bundled treebox sandbox image.
#
# Every build input is pinned, so two builds of this file a week apart install
# the same tools: the base image by its multi-arch index digest, every download
# by version and SHA-256 (one checksum for each architecture, amd64 and arm64),
# and every npm package by exact version. Debian packages come from the signed
# Debian archive of the base image's release and take its security updates.
#
# The hosts a build needs, where each pin comes from, and the update policy are
# in docs/configuration.md ("Build inputs"). A build that cannot reach a
# required host fails with a "treebox:" message that names that host.
FROM python:3.14.6-slim-trixie@sha256:7bec7ddcddeff7975d6ba9b4be7dd6f6b2f55e7491539145e2978f7f97ce9144

ARG TZ=UTC
ARG USER_UID=1000
ARG USER_GID=1000
ARG USERNAME=agent

ENV TZ="$TZ"
ENV DEBIAN_FRONTEND=noninteractive

# Build helpers, plain files of this template:
# - treebox-fetch URL SHA256 FILE: download URL to FILE, then check its SHA-256.
#   A failed download names the host it could not reach (after redirects).
# - treebox-online HOST[,HOST...] COMMAND...: run COMMAND, which downloads from
#   package registries; if it fails, name the hosts it needs.
# This file uses no BuildKit-only syntax (no heredoc, no COPY --chmod), so it
# also builds with Docker's legacy builder, on a host without docker-buildx.
COPY treebox-fetch /usr/local/bin/treebox-fetch
COPY treebox-online /usr/local/bin/treebox-online
RUN chmod 0755 /usr/local/bin/treebox-fetch /usr/local/bin/treebox-online

# apt-get update exits 0 when it cannot fetch an index; --error-on=any makes an
# unreachable archive fail here instead of as a later "Unable to locate package".
RUN treebox-online deb.debian.org apt-get update --error-on=any \
  && treebox-online deb.debian.org apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg2 less git procps sudo zsh bsdextrautils \
    unzip zip tar gzip bzip2 xz-utils nano jq yq rsync ripgrep fd-find \
    iptables ipset iproute2 dnsutils aggregate openssh-client socat \
    gcc g++ make pkg-config bubblewrap \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Node.js (with npm and corepack): the official release tarball.
# Checksums: https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt
ARG NODE_VERSION=22.23.5
ARG NODE_SHA256_AMD64=cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8
ARG NODE_SHA256_ARM64=2babb9eb5264860d540849f4b064a6fbdebcdc323657d9d382ce23d313fd5e09
RUN case "$(dpkg --print-architecture)" in \
      amd64) NODE_ARCH=x64 NODE_SHA256="$NODE_SHA256_AMD64" ;; \
      arm64) NODE_ARCH=arm64 NODE_SHA256="$NODE_SHA256_ARM64" ;; \
      *) echo "treebox: unsupported architecture $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac && \
    treebox-fetch "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
      "$NODE_SHA256" /tmp/node.tar.xz && \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner && \
    rm /tmp/node.tar.xz && \
    node --version && npm --version

# GitHub CLI: the release tarball, plus the shell completions its .deb ships.
# Checksums: https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt
ARG GH_VERSION=2.104.0
ARG GH_SHA256_AMD64=98cb101ad87dbbd3f6b2f2bfcbd43197ccd07643140121d8a9cfaa91eadbc24e
ARG GH_SHA256_ARM64=cb34cefc6f4e77b90731374a2b737516bb8f3033c6ca46aa949e5344f68069e3
RUN ARCH=$(dpkg --print-architecture) && \
    case "$ARCH" in \
      amd64) GH_SHA256="$GH_SHA256_AMD64" ;; \
      arm64) GH_SHA256="$GH_SHA256_ARM64" ;; \
      *) echo "treebox: unsupported architecture $ARCH" >&2; exit 1 ;; \
    esac && \
    treebox-fetch "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${ARCH}.tar.gz" \
      "$GH_SHA256" /tmp/gh.tar.gz && \
    tar -xzf /tmp/gh.tar.gz -C /tmp && \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_${ARCH}/bin/gh" /usr/local/bin/gh && \
    mkdir -p /usr/share/bash-completion/completions \
      /usr/share/zsh/vendor-completions /usr/share/fish/vendor_completions.d && \
    gh completion -s bash > /usr/share/bash-completion/completions/gh && \
    gh completion -s zsh > /usr/share/zsh/vendor-completions/_gh && \
    gh completion -s fish > /usr/share/fish/vendor_completions.d/gh.fish && \
    rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_${ARCH}" && \
    gh --version

RUN if ! getent group "${USER_GID}" >/dev/null; then groupadd --gid "${USER_GID}" "${USERNAME}"; fi && \
    useradd -m -u "${USER_UID}" -g "${USER_GID}" -s /bin/zsh "${USERNAME}" && \
    echo "${USERNAME} ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/${USERNAME}-firewall && \
    chmod 0440 /etc/sudoers.d/${USERNAME}-firewall

RUN mkdir -p /workspace /home/${USERNAME}/.codex /home/${USERNAME}/.claude /commandhistory && \
    chown -R ${USERNAME}:${USER_GID} /workspace /home/${USERNAME} /commandhistory

RUN echo "export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
      >> /home/${USERNAME}/.bashrc && \
    echo "export HISTFILE=/commandhistory/.zsh_history" \
      >> /home/${USERNAME}/.zshrc && \
    touch /commandhistory/.bash_history /commandhistory/.zsh_history && \
    chown -R ${USERNAME}:${USER_GID} /commandhistory /home/${USERNAME}/.bashrc /home/${USERNAME}/.zshrc

USER ${USERNAME}
ENV PATH="/home/${USERNAME}/.local/bin:${PATH}"

# uv and uvx: the release tarball, into ~/.local/bin (where uv's installer puts them).
# Checksums: https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/<asset>.sha256
ARG UV_VERSION=0.13.8
ARG UV_SHA256_AMD64=5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91
ARG UV_SHA256_ARM64=74f2f5c6024d397481db228db8fc25eed420db192243f88438c118e7e013e585
RUN case "$(dpkg --print-architecture)" in \
      amd64) UV_TRIPLE=x86_64-unknown-linux-gnu UV_SHA256="$UV_SHA256_AMD64" ;; \
      arm64) UV_TRIPLE=aarch64-unknown-linux-gnu UV_SHA256="$UV_SHA256_ARM64" ;; \
      *) echo "treebox: unsupported architecture $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac && \
    treebox-fetch "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_TRIPLE}.tar.gz" \
      "$UV_SHA256" /tmp/uv.tar.gz && \
    mkdir -p "$HOME/.local/bin" && \
    tar -xzf /tmp/uv.tar.gz -C "$HOME/.local/bin" --strip-components=1 "uv-${UV_TRIPLE}/uv" "uv-${UV_TRIPLE}/uvx" && \
    rm /tmp/uv.tar.gz && \
    uv --version

# The steps below are ordered from the one that changes least to the one that
# changes most (the agent CLIs are bumped at least monthly), so a bump rebuilds
# only its own step and the ones after it.

# Playwright: exact npm versions (each pins its own Chromium build), the
# browser's system libraries from Debian, and the browser from Playwright's CDN.
# Only Chromium's headless shell is installed: the container has no display,
# so every supported use (playwright-cli, `playwright screenshot`, scripts) is
# headless. Firefox, WebKit and the full Chromium build are not installed.
# playwright-cli opens the "chrome" channel, which it finds at
# /opt/google/chrome/chrome: that path points to the headless shell.
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ARG PLAYWRIGHT_CLI_VERSION=0.1.24
ARG PLAYWRIGHT_VERSION=1.65.0
RUN treebox-online registry.npmjs.org \
      npm install -g "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}" "playwright@${PLAYWRIGHT_VERSION}" && \
    npm cache clean --force && \
    mkdir -p /ms-playwright && \
    treebox-online deb.debian.org playwright install-deps chromium && \
    treebox-online cdn.playwright.dev playwright-cli install-browser --only-shell chromium && \
    chown -R ${USERNAME}:${USER_GID} /ms-playwright && \
    chmod -R u+rwX,go+rX /ms-playwright && \
    CHROMIUM_BIN="$(find /ms-playwright -type f -name chrome-headless-shell -path '*chromium_headless_shell-*' | head -1)" && \
    if [ -z "$CHROMIUM_BIN" ]; then echo "treebox: no Chromium headless shell found under /ms-playwright" >&2; ls -R /ms-playwright >&2; exit 1; fi && \
    mkdir -p /opt/google/chrome && \
    ln -sfn "$CHROMIUM_BIN" /opt/google/chrome/chrome && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Claude Code: the native build for this version, checked against the SHA-256
# in https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/manifest.json.
# `claude install` then finds that version already in place and only sets up
# the launcher, so it downloads nothing.
USER ${USERNAME}
ARG CLAUDE_CODE_VERSION=2.1.311
ARG CLAUDE_CODE_SHA256_AMD64=86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada
ARG CLAUDE_CODE_SHA256_ARM64=2e2496c9a8065df93acb0fca2d341f59a6b8777649b2e025ab8c9bcd5eef61f3
RUN case "$(dpkg --print-architecture)" in \
      amd64) CLAUDE_PLATFORM=linux-x64 CLAUDE_CODE_SHA256="$CLAUDE_CODE_SHA256_AMD64" ;; \
      arm64) CLAUDE_PLATFORM=linux-arm64 CLAUDE_CODE_SHA256="$CLAUDE_CODE_SHA256_ARM64" ;; \
      *) echo "treebox: unsupported architecture $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac && \
    CLAUDE_BIN="$HOME/.local/share/claude/versions/${CLAUDE_CODE_VERSION}" && \
    mkdir -p "$(dirname "$CLAUDE_BIN")" && \
    treebox-fetch "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/${CLAUDE_PLATFORM}/claude" \
      "$CLAUDE_CODE_SHA256" "$CLAUDE_BIN" && \
    chmod 0755 "$CLAUDE_BIN" && \
    treebox-online downloads.claude.ai "$CLAUDE_BIN" install "${CLAUDE_CODE_VERSION}" && \
    claude --version

# Codex CLI: an exact npm version. Its platform binary is an exact-version
# optional dependency, so the whole install is pinned. The npm cache would
# keep a second, compressed copy of that binary in the image, so it is removed.
USER root
ARG CODEX_CLI_VERSION=0.167.2
RUN treebox-online registry.npmjs.org npm install -g "@openai/codex@${CODEX_CLI_VERSION}" && \
    npm cache clean --force && \
    codex --version

# Setup hooks are baked into the image, never run from the mounted worktree, so a
# boxed agent cannot edit what runs at container setup.
COPY init-firewall.sh /usr/local/bin/init-firewall.sh
COPY allowed-domains.sh /usr/local/share/allowed-domains.sh
COPY post-create.sh /usr/local/bin/post-create.sh
RUN chmod +x /usr/local/bin/init-firewall.sh /usr/local/bin/post-create.sh

USER ${USERNAME}

ENV SHELL=/bin/zsh
ENV EDITOR=nano
ENV VISUAL=nano
ENV CODEX_HOME=/home/${USERNAME}/.codex
ENV CLAUDE_CONFIG_DIR=/home/${USERNAME}/.claude
ENV NODE_OPTIONS="--max-old-space-size=4096"

WORKDIR /workspace
