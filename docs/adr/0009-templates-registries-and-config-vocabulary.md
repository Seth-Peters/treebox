# Templates, registries, and config vocabulary

The names treebox understands (harnesses, isolation modes, templates, config
keys) are closed sets, each defined in one place. Names stay plain strings
at the boundaries and are resolved once to objects. Sandbox templates are
operator-owned copies managed through the `template` commands, with a
replacement that can always be rolled back.

## Decisions

### Boundary values stay `str`; deeper seams take resolved objects

Values that cross a boundary (TOML, CLI flags, state files) remain plain
`str`. They are validated against the registry-derived `VALID_*` tuples and
resolved once to `Harness` and `Runner` objects; internal seams take the
resolved object. In `create`, for example, the harness string was already
validated by `_resolve_config`, and `get_harness` turns it into the object
that provisioning receives.

The `Isolation` and `Harness` `Literal` aliases are hard-coded so static
typing works. A drift test, `test_registry_vocabularies_cannot_drift`,
asserts that each alias still names the same set as its registry.

Where: `config.py` · `Isolation`, `Harness`; `cli.py` · `create`
(`get_harness`)

### The harness registry's order is user-facing

`HARNESSES` is an ordered tuple. Doctor's login rows and the help text list
harnesses in this order, and the first entry is the default whose CLI the
no-login advice names. Reordering it changes what users see.

Where: `harnesses.py` · `HARNESSES`

### The login dir is stored un-expanded

A harness keeps its host login dir in the `~/` form. Human output (doctor
advice, launch errors) then shows the familiar spelling instead of an
expanded absolute path.

Where: `harnesses.py` · `Harness._credential_dir`

### Only named login files are staged into a sandbox

`_credential_files` lists the only files copied into a sandbox: what the
agent needs to authenticate, plus its user-level settings. Everything else
in the live dir (state, history, host-executed hooks) stays on the host. One
of the staged files, `_login_file`, holds the subscription login itself (a
JSON object); the others are settings.

Where: `harnesses.py` · `Harness._credential_files`, `Harness._login_file`

### The Keychain service is optional per harness

`_keychain_service` names the macOS Keychain service where the agent CLI
keeps its login on that platform instead of in the login file. `None` means
the file is the only store.

Where: `harnesses.py` · `Harness._keychain_service`

### User config keys are an explicit allowlist

`USER_KEYS` lists the top-level keys a user config may set. Each one is the
`Config` field of the same name. Every other `Config` field is internal: the
CLI sets it and it is never read from TOML, so a config file that names one
is rejected as an unknown key.

Where: `config.py` · `USER_KEYS`

### The `container.json` schema is treebox-owned

The docker runner renders and runs a container definition named
`container.json`. Its schema belongs to treebox:
`build.{dockerfile,args}`, `user`, `mounts`, `env`, `runArgs`, `postCreate`.

Where: `assets.py` · `CONFIG_FILE`

### Build helper files are optional in a template

`treebox-fetch` and `treebox-online` are build helpers the bundled
Dockerfile copies in. The docker runner renders them into the build context
when the template has them. They are not required: a template whose
Dockerfile does not use them (one scaffolded before they existed, or written
from scratch) stays valid.

Where: `assets.py` · `TEMPLATE_HELPER_FILES`

### The bundled template is materialized once per process

`importlib.resources.as_file` may materialize the bundled template to a
temporary location (zipped installs, unusual loaders) that is cleaned up
when its context exits. The context must therefore outlive every caller of
the returned path. One process-lifetime `ExitStack`, closed at exit, owns
the materialization, and a cache makes repeated resolutions reuse it instead
of extracting again.

Where: `assets.py` · `_RESOURCE_LIFETIME`, `_bundled_template_dir`

### Template commands are the sanctioned way to own a template

Customizing the docker sandbox means owning a copy of the shipped template.
The `template` commands are the way to get and inspect one. The alternative,
reaching into the package with a `python -c 'import treebox...'` one-liner,
is rejected: it does not even run under an isolated tool install. Every
command derives from `assets.template_dir()`, the single resolver the docker
runner already uses.

Where: `cli.py` · `template_app`

### A reserved staging directory is never a template

`.treebox-tmp` under `user_templates_root()` is where `template init` builds
a new template and parks the old one during a `--force` swap. It is not a
template: `available_templates` skips it and `template init` refuses the
name, so a leftover from an interrupted run never shows up as one.

More generally, only directories whose names are valid template identifiers
are listed, because only identifiers are selectable.

Where: `assets.py` · `STAGING_DIR`, `available_templates`

### `template init --force` keeps the old template until the new one is in

The old template is kept byte-for-byte until the new copy is installed, so a
rollback needs only a rename. Each run uses a unique staging directory under
`STAGING_DIR`, because another process may still own a directory left by an
earlier run.

If the rollback rename itself fails, the staging directory is not deleted:
it then holds the only copy of the old template, and the error message says
where it was kept.

Where: `cli.py` · `template_init`

### `template list` shows highlights only for the bundled default

`DEFAULT_TEMPLATE_HIGHLIGHTS` is a curated list of the main things the
bundled default image ships (taken from its Dockerfile), shown under
`template list` so an operator can see what the stock sandbox gives them
without reading the build recipe. It describes the bundled default only. A
user template is whatever its owner made it, so the highlights are shown
only when `default` still resolves to the bundled image. A user override or
`$TREEBOX_TEMPLATE_DIR` replaces it with a box whose contents treebox cannot
vouch for.

Where: `assets.py` · `DEFAULT_TEMPLATE_HIGHLIGHTS`; `cli.py` ·
`template_list`

### `template list` skips a name that vanishes mid-listing

Enumerated template names resolve by construction. If one fails to resolve
anyway, a template was removed between enumeration and resolution, and the
row is skipped instead of failing the whole listing.

Where: `cli.py` · `template_list`

### `enter` tolerates a deleted or broken template

When `enter` compares the recorded limits with what config and template now
give, a template that no longer resolves or no longer parses is treated as
setting no limits. The worktree's recorded limits are authoritative, so a
missing template must not block entry to an existing worktree.

Where: `cli.py` · `_check_entry_resources`
