# Golden CLI-output snapshots

Reference outputs for the CLI's observable surface: `doctor`,
`create --print` / `--dry-run`, `enter --print`, and their `--json` forms, for
both isolations. Machine-specific bits (paths, uid/gid, git version, container
name digests) are normalized to `__…__` placeholders.

Every behavior-preserving change must leave these byte-identical:

```bash
scripts/golden-diff.sh            # the gate: diffs live output against these files
scripts/golden-diff.sh --update   # regenerate — only after deciding to change behavior
```

`scripts/validate.sh` runs the diff gate as part of full local and CI
validation.

A diff here means observable CLI behavior changed. Under the output-stability
contract that is a bug, not a snapshot to refresh — unless the change is
intentional.
