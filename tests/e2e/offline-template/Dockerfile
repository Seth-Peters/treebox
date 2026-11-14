# Offline treebox e2e sandbox. Once the base image is present, this build
# needs no network: every step below only edits local files. The agent is the
# controlled stub (tests/e2e/stub-agent), shipped as post-create.sh and linked
# as claude and codex, so a launch in this sandbox never starts a real agent.
#
# The base image is pinned by digest (alpine/git 2.54.0, multi-arch index) so
# a run is reproducible and the local copy satisfies the build offline.
FROM alpine/git:2.54.0@sha256:6a1fdb962814ca11508ca8d1bab94375b5b7ac4e4a9a40981c29fc32c02f095d

ARG TZ=UTC
ARG USER_UID=1000
ARG USER_GID=1000
ENV TZ="$TZ"

# The agent user mirrors the host uid/gid so bind-mounted files keep their
# owner. Plain /etc/passwd edits, no package tools: a gid that already exists
# is reused, and uid 0 (a root host) becomes a second name for root.
RUN set -eu; \
    if ! cut -d: -f3 /etc/group | grep -qx "$USER_GID"; then \
      echo "agent:x:${USER_GID}:" >> /etc/group; \
    fi; \
    echo "agent:x:${USER_UID}:${USER_GID}:agent:/home/agent:/bin/sh" >> /etc/passwd; \
    mkdir -p /home/agent/.claude /home/agent/.codex; \
    chown -R "${USER_UID}:${USER_GID}" /home/agent

COPY post-create.sh /usr/local/bin/post-create.sh
RUN chmod 0755 /usr/local/bin/post-create.sh \
  && ln -s post-create.sh /usr/local/bin/claude \
  && ln -s post-create.sh /usr/local/bin/codex

# alpine/git's entrypoint is `git`; treebox runs its own keep-alive command.
ENTRYPOINT []
WORKDIR /
