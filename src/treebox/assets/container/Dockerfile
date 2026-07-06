FROM python:3.14.6-slim

ARG TZ=UTC
ARG USER_UID=1000
ARG USER_GID=1000
ARG USERNAME=agent
ARG NODE_MAJOR=22
ARG CODEX_CLI_VERSION=latest
ARG CLAUDE_CODE_VERSION=latest
ARG GIT_DELTA_VERSION=0.18.2

ENV TZ="$TZ"
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg2 less git procps sudo fzf zsh man-db \
    unzip zip tar gzip bzip2 xz-utils nano vim jq yq rsync ripgrep fd-find \
    iptables ipset iproute2 dnsutils aggregate openssh-client socat \
    gcc g++ make cmake pkg-config python3-dev python3-venv bubblewrap \
  && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
  && mkdir -p -m 755 /etc/apt/keyrings \
  && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
  && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
  && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(dpkg --print-architecture) && \
    wget -q "https://github.com/dandavison/delta/releases/download/${GIT_DELTA_VERSION}/git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
    dpkg -i "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
    rm "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb"

RUN ARCH=$(dpkg --print-architecture) && \
    case "$ARCH" in \
      amd64) AWS_ARCH=x86_64 ;; \
      arm64) AWS_ARCH=aarch64 ;; \
      *) echo "unsupported arch $ARCH" && exit 1 ;; \
    esac && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws

RUN if ! getent group "${USER_GID}" >/dev/null; then groupadd --gid "${USER_GID}" "${USERNAME}"; fi && \
    useradd -m -u "${USER_UID}" -g "${USER_GID}" -s /bin/zsh "${USERNAME}" && \
    echo "${USERNAME} ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/${USERNAME}-firewall && \
    chmod 0440 /etc/sudoers.d/${USERNAME}-firewall

RUN mkdir -p /workspace /home/${USERNAME}/.codex /home/${USERNAME}/.claude /home/${USERNAME}/.aws /commandhistory && \
    chown -R ${USERNAME}:${USER_GID} /workspace /home/${USERNAME} /commandhistory

RUN echo "export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
      >> /home/${USERNAME}/.bashrc && \
    echo "export HISTFILE=/commandhistory/.zsh_history" \
      >> /home/${USERNAME}/.zshrc && \
    touch /commandhistory/.bash_history /commandhistory/.zsh_history && \
    chown -R ${USERNAME}:${USER_GID} /commandhistory /home/${USERNAME}/.bashrc /home/${USERNAME}/.zshrc

USER ${USERNAME}

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/home/${USERNAME}/.local/bin:${PATH}"

USER root
RUN npm install -g "@openai/codex@${CODEX_CLI_VERSION}" && codex --version

USER ${USERNAME}
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} && claude --version

USER root

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN npm install -g @playwright/cli playwright && \
    mkdir -p /ms-playwright && \
    npx playwright install-deps chromium && \
    playwright-cli install-browser && \
    chown -R ${USERNAME}:${USER_GID} /ms-playwright && \
    chmod -R u+rwX,go+rX /ms-playwright && \
    CHROMIUM_BIN="$(find /ms-playwright -type f -name chrome -path '*chromium-*' | head -1)" && \
    if [ -z "$CHROMIUM_BIN" ]; then echo "treebox: no Chromium binary found under /ms-playwright" >&2; ls -R /ms-playwright >&2; exit 1; fi && \
    mkdir -p /opt/google/chrome && \
    ln -sfn "$CHROMIUM_BIN" /opt/google/chrome/chrome && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

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
