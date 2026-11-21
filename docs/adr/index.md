# Decision records

The reasons behind treebox's non-obvious choices. The source carries no
comments and no docstrings (`scripts/comment_ratchet.py` enforces it), so a
reason that a name, a type or a test cannot carry lives here. Each record
groups the decisions of one theme, and each decision names the module and
symbol it governs.

- [0001 · Host-owned state and the sandbox trust boundary](0001-host-owned-state-and-the-sandbox-trust-boundary.md)
- [0002 · CLI contract: exit codes, JSON, and unattended use](0002-cli-contract-exit-codes-json-and-unattended-use.md)
- [0003 · Locking and concurrent operations](0003-locking-and-concurrent-operations.md)
- [0004 · Create: freshness, branch naming, and resume](0004-create-freshness-branch-naming-and-resume.md)
- [0005 · Teardown safety, ref resolution, and worktree status](0005-teardown-safety-ref-resolution-and-worktree-status.md)
- [0006 · Runner mechanics: setup, entry, and agent sessions](0006-runner-mechanics-setup-entry-and-agent-sessions.md)
- [0007 · Terminal output and interactive rendering](0007-terminal-output-and-interactive-rendering.md)
- [0008 · Ports, resource limits, and worktree identity](0008-ports-resource-limits-and-worktree-identity.md)
- [0009 · Templates, registries, and config vocabulary](0009-templates-registries-and-config-vocabulary.md)
- [0010 · Factory CLI](0010-factory-cli.md)
- [0011 · Product principles](0011-product-principles.md)
- [0012 · The bundled sandbox image](0012-the-bundled-sandbox-image.md)
- [0013 · Verification and CI](0013-verification-and-ci.md)
- [0014 · The Runner protocol contract](0014-the-runner-protocol-contract.md)

To add a decision, append it to the record whose theme it belongs to. Start
a new record, with the next number, only for a new theme.
