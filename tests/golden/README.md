# Golden CLI-output snapshots (issue #141)

Reference outputs captured on `main` before the deep-module refactor: `doctor`,
`create --print` / `--dry-run`, `enter --print`, and their `--json` forms, for
both isolations. Machine-specific bits (paths, uid/gid, git version, container
name digests) are normalized to `__…__` placeholders.

Every PR under issue #141 must leave these byte-identical:

```bash
scripts/golden-diff.sh            # the gate: diffs live output against these files
scripts/golden-diff.sh --update   # regenerate — only after a decision on the issue
```

`scripts/validate.sh` runs the diff gate as part of full local and CI
validation.

A diff here means observable CLI behavior changed. Under the issue's stability
contract that is a bug in the refactor, not a snapshot to refresh.
