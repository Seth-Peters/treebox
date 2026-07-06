# Roadmap

treebox is intentionally small: isolated git worktrees, cache-backed setup, and
host or Docker runners for AI coding agents. A few directions we'd like to grow in,
roughly in priority order:

1. **Support more coding-agent CLIs.** Extend the `harnesses.py` registry
   beyond `claude`/`codex` so more agents can launch in a provisioned worktree.
2. **Support more isolation environments.** Add runners beyond host and Docker —
   Podman first, as a rootless, daemonless drop-in.
3. **Wider support and testing for different git services.** Exercise and harden
   the git handling against more hosts (GitHub, GitLab, Bitbucket, self-hosted)
   and remote configurations.
4. **More examples for customizing the isolation environment.** Grow the docs with
   additional sandbox-template recipes (languages, toolchains, base images).

Nothing here is a commitment or a timeline — it's a sketch of where treebox is
likely to head. Ideas and issues are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
