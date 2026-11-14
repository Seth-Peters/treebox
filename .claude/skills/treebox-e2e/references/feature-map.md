# Treebox feature map

One row per user-visible route: the route, where it lives in the source, and
the scenarios that drive it. The scenario text (command, pass criterion) lives
only in `scenarios.md`; the checks live only in `tests/e2e/scenarios.py`.

Source references are `path::symbol` under `src/treebox/` (`Class.method` for
a method). `tests/test_e2e_map.py` (part of `validate.sh`) fails when:

- a CLI command or option, a `--json` error code (the collector that also
  keeps the error-code table in `docs/usage.md` complete, in
  `tests/test_contract.py`), a config key, or an environment variable that the
  source reads or a `TREEBOX_*` name it exports has no row here, or a row
  names one that no longer exists;
- a source reference names a file or symbol that does not exist;
- a row names a scenario id that `scenarios.md` or `tests/e2e/scenarios.py`
  does not have, or a scenario is in one of them but not the other, or no row
  names it.

**Adding a route** is one row here plus one scenario (prose in
`scenarios.md`, checks with the same id in `tests/e2e/scenarios.py`). A
`behavior …` row is free-form: use it for a route that is not a flag, an error
code, a config key or an environment variable.

## Commands and options

| Route | Source | Scenarios |
| --- | --- | --- |
| `--version` | `cli.py::_version_callback` | A1 |
| `--non-interactive` | `cli.py::main`, `cli.py::_interactive` | U2, B25 |
| `--install-completion` | `cli.py::app` | A7 |
| `--show-completion` | `cli.py::app` | A7 |
| `create` | `cli.py::create`, `provision.py::create` | B1, B2, B3, B7, B8 |
| `create --checkout` | `cli.py::create`, `provision.py::create` | B5, B17, R37 |
| `create --repo` | `cli.py::_repo_root` | B1, B19 |
| `create --root` | `models.py::worktree_root` | B1, B6 |
| `create --base` | `provision.py::create` | B4, G4 |
| `create --isolation` | `runners/__init__.py::get_runner` | H1, B22 |
| `create --harness` | `harnesses.py::get_harness` | B12 |
| `create --cold` | `runners/host.py::HostRunner.setup` | B24, R39 |
| `create --no-fetch` | `git.py::fetch_origin` | B9 |
| `create --firewall` | `runners/docker.py::DockerRunner._overlaid_config` | G10 |
| `create --template` | `assets.py::template_dir` | H1, H3, B21 |
| `create --cpus` | `cli.py::_creation_limits`, `resources.py::parse_cpus`, `runners/docker.py::DockerRunner.check_resources` | B26, B23, H12 |
| `create --memory` | `cli.py::_creation_limits`, `resources.py::parse_memory` | B26, H12 |
| `create --pids-limit` | `cli.py::_creation_limits`, `resources.py::parse_pids` | B26, H12 |
| `create --dry-run` | `cli.py::_dry_run`, `provision.py::dry_run_plan` | B10, B10b, R39 |
| `create --print` | `cli.py::_emit_result` | B11 |
| `create --json` | `cli.py::_emit_result` | B1 |
| `create --quiet` | `output.py::Reporter` | B14 |
| `create --verbose` | `output.py::Reporter` | B14 |
| `enter` | `cli.py::enter`, `provision.py::enter` | C1, C8, C18 |
| `enter --repo` | `cli.py::_repo_root` | C1 |
| `enter --root` | `resolve.py::resolve_ref` | C1, R43 |
| `enter --isolation` | `cli.py::_reconcile_with_record` | C7, H4 |
| `enter --harness` | `cli.py::_reconcile_with_record` | C10 |
| `enter --template` | `cli.py::_reconcile_with_record` | H11 |
| `enter --cpus` | `cli.py::_check_entry_resources` | H12 |
| `enter --memory` | `cli.py::_check_entry_resources` | H12 |
| `enter --pids-limit` | `cli.py::_check_entry_resources` | H12 |
| `enter --cold` | `provision.py::enter` | C11 |
| `enter --print` | `cli.py::_emit_result` | C1, C6 |
| `enter --json` | `cli.py::_emit_result` | C4 |
| `enter --quiet` | `output.py::Reporter` | C12 |
| `enter --verbose` | `output.py::Reporter` | C12 |
| `list` | `cli.py::list_cmd`, `cli.py::_collect_rows` | D1, D2 |
| `list --repo` | `cli.py::_repo_root` | D1 |
| `list --root` | `models.py::worktree_root` | D3 |
| `list --json` | `cli.py::list_cmd` | D2 |
| `ls` | `cli.py::list_cmd` | D4 |
| `teardown` | `cli.py::teardown`, `cleanup.py::cleanup_one` | E2, E5, E6, C18 |
| `teardown --repo` | `cli.py::_repo_root` | E1 |
| `teardown --root` | `resolve.py::resolve_ref` | R43 |
| `teardown --yes` | `cli.py::teardown` | E1 |
| `teardown --delete-branch` | `cli.py::_teardown_one` | E4, I5 |
| `teardown --remove-volumes` | `runners/docker.py::DockerRunner.teardown` | H6 |
| `teardown --force` | `cli.py::teardown` | E2, E3, E7 |
| `teardown --skip-container` | `cli.py::_teardown_one` | H6 |
| `teardown --json` | `cli.py::TeardownRecord` | E4 |
| `teardown --quiet` | `output.py::Reporter` | E11 |
| `teardown --verbose` | `output.py::Reporter` | E11 |
| `rm` | `cli.py::teardown` | E8 |
| `doctor` | `cli.py::doctor` | A2 |
| `doctor --repo` | `cli.py::doctor` | A2 |
| `doctor --isolation` | `cli.py::doctor` | A4, A5, B23 |
| `doctor --harness` | `cli.py::doctor`, `cli.py::_agent_binary_check` | A9 |
| `doctor --json` | `cli.py::doctor` | A3 |
| `version` | `cli.py::version` | A1 |
| `template` | `cli.py::template_app` | A6, A8 |
| `template init` | `cli.py::template_init` | F3 |
| `template init --from` | `cli.py::template_init` | F5, F6 |
| `template init --force` | `cli.py::template_init` | F4, F9 |
| `template init --json` | `cli.py::template_init` | F3 |
| `template list` | `cli.py::template_list` | F1 |
| `template list --json` | `cli.py::template_list` | F1 |
| `template ls` | `cli.py::template_list` | F7 |
| `template path` | `cli.py::template_path` | F2 |

## Error codes (`--json` `error.code`)

| Route | Source | Scenarios |
| --- | --- | --- |
| `error AMBIGUOUS_REF` | `resolve.py::AmbiguousRefError` | C5 |
| `error BRANCH_EXISTS` | `provision.py::BranchConflictError` | B6, B10b |
| `error BRANCH_IN_USE` | `provision.py::BranchInUseError` | B17, B10b |
| `error CANCELLED` | `cli.py::_run_session`, `system.py::run_agent` | U4, H17 |
| `error COPY_FAILED` | `cli.py::template_init` | F9 |
| `error DESTINATION_CONFLICT` | `provision.py::DestinationConflictError` | R45 |
| `error DIRTY_WORKTREE` | `cli.py::teardown` | E2 |
| `error DOCKER_UNAVAILABLE` | `runners/docker.py::DockerRunner.preflight` | B23 |
| `error ERROR` | `cli.py::_classify`, `runners/docker.py::DockerRunner.prepare_entry` | H9, B16 |
| `error FETCH_FAILED` | `git.py::FetchError` | B9 |
| `error FIREWALL_FAILED` | `runners/base.py::FirewallError`, `runners/docker.py::DockerRunner.prepare_entry` | H14 |
| `error GIT_MISSING` | `git.py::GitMissingError` | B20 |
| `error HARNESS_MISSING` | `cli.py::_require_agent`, `runners/docker.py::DockerRunner.launch` | B16, H16 |
| `error INVALID_BRANCH` | `cli.py::_validate_branch` | B18 |
| `error INVALID_CONFIG` | `cli.py::_resolve_config`, `config.py::load_config` | G1, G3, G12, G16 |
| `error INVALID_LINKAGE` | `provision.py::InvalidLinkageError` | C15 |
| `error INVALID_NAME` | `cli.py::_validate_name`, `cli.py::template_init` | B8, F8 |
| `error INVALID_RESOURCE` | `resources.py::ResourceLimitError`, `cli.py::_resource_flags`, `cli.py::_run_session` | B26 |
| `error INVALID_ROOT` | `cli.py::_check_root` | B27 |
| `error ISOLATION_MISMATCH` | `cli.py::_reconcile_with_record` | C7 |
| `error LOCK_HELD` | `locking.py::worktree_lock` | P3 |
| `error LOGIN_MISSING` | `cli.py::_require_agent`, `cli.py::_agent_login_check` | B25 |
| `error MAIN_WORKTREE` | `cleanup.py::MainWorktreeError` | E9 |
| `error MISSING_DEPENDENCY` | `runners/base.py::PreflightError` | B22, B26 |
| `error NEEDS_CONFIRMATION` | `cli.py::teardown` | E1, E7, U2 |
| `error NOT_A_REPO` | `cli.py::_repo_root` | B19 |
| `error NOT_FOUND` | `provision.py::NotFoundError` | C5, B10b, C18 |
| `error OVERWRITE_FAILED` | `cli.py::template_init` | F9 |
| `error PORTS_EXHAUSTED` | `ports.py::PortsExhaustedError` | G14 |
| `error PORT_LEDGER_INVALID` | `ports.py::LedgerInvalidError` | G15 |
| `error RECORD_CONFLICT` | `provision.py::RecordConflictError` | C17 |
| `error RECORD_INVALID` | `record.py::RecordInvalidError` | C9 |
| `error RECORD_MISSING` | `record.py::RecordMissingError` | C16 |
| `error RESOURCE_MISMATCH` | `cli.py::_check_entry_resources` | H12 |
| `error RESOURCE_UNSUPPORTED` | `cli.py::_refuse_unsupported_limits` | B26 |
| `error RUNNING_AS_ROOT` | `runners/docker.py::DockerRunner.preflight` | B29 |
| `error SETUP_FAILED` | `provision.py::SetupFailedError` | C14, C17 |
| `error SLUG_CONFLICT` | `provision.py::SlugConflictError` | B7, R38 |
| `error SUBMODULE_COPY_FAILED` | `provision.py::SubmoduleCopyFailedError` | R45b |
| `error TEARDOWN_INCOMPLETE` | `cleanup.py::CleanupResult`, `cli.py::teardown` | H10 |
| `error TEMPLATE_CONFLICT` | `cli.py::template_init` | F8 |
| `error TEMPLATE_EXISTS` | `cli.py::template_init` | F4 |
| `error TEMPLATE_INVALID` | `assets.py::TemplateInvalidError` | B21 |
| `error TEMPLATE_NOT_FOUND` | `assets.py::TemplateNotFoundError` | F6, H3, G11 |
| `error WORKTREE_LOCKED` | `cli.py::teardown` | E10 |

## Config keys (`config.toml`)

| Route | Source | Scenarios |
| --- | --- | --- |
| `config isolation` | `config.py::Config` | G3 |
| `config harness` | `config.py::Config` | G5 |
| `config base` | `config.py::Config` | G2, G4 |
| `config root` | `config.py::Config`, `models.py::worktree_root` | G6 |
| `config env_file` | `config.py::Config`, `provision.py::resolve_env_file` | G7 |
| `config firewall` | `config.py::Config` | G10 |
| `config template` | `config.py::Config`, `assets.py::template_dir` | G11 |
| `config setup_hook` | `config.py::Config`, `runners/host.py::HostRunner._run_override` | G8 |
| `config caches` | `config.py::default_caches` | G9 |
| `config resources` | `config.py::Config`, `resources.py::from_mapping` | G16, H12 |
| `config ports` | `config.py::PortRange`, `ports.py::reserve` | G14 |

## Environment variables

| Route | Source | Scenarios |
| --- | --- | --- |
| `env TREEBOX_HOME` | `config.py::treebox_home` | F3 |
| `env TREEBOX_CONFIG` | `config.py::config_path` | G1 |
| `env TREEBOX_TEMPLATE_DIR` | `assets.py::template_dir` | G13 |
| `env XDG_CACHE_HOME` | `config.py::default_caches` | G9 |
| `env TZ` | `runners/docker.py::DockerRunner._inject` | G13 |
| `env TREEBOX_NON_INTERACTIVE` | `cli.py::main` | U2, B25 |
| `env TREEBOX_NAME` | `models.py::Worktree.treebox_env` | B15, H7 |
| `env TREEBOX_ID` | `models.py::worktree_id`, `models.py::Worktree.treebox_env` | B1, B15, H7 |
| `env TREEBOX_PORT_BASE` | `ports.py::reserve`, `models.py::Worktree.treebox_env` | B1, B15, G14, H7 |
| `env TREEBOX_PORT_COUNT` | `models.py::Worktree.treebox_env` | B15 |

## Behaviors

| Route | Source | Scenarios |
| --- | --- | --- |
| `behavior help and bare invocation` | `cli.py::app` | A8 |
| `behavior shell completion protocol` | `cli.py::app` | A6 |
| `behavior doctor checks` | `cli.py::doctor`, `cli.py::_doctor_problems` | A2, A3, A4, A9 |
| `behavior macOS Keychain login` | `harnesses.py::Harness.login`, `harnesses.py::Harness.stage_credentials` | A10, H13 |
| `behavior real launch on the host` | `runners/host.py::HostRunner.launch` | B15, B16, C8 |
| `behavior real launch in a container` | `runners/docker.py::DockerRunner.launch` | H7, H16 |
| `behavior agent exit line` | `cli.py::AGENT_EXIT_PREFIX`, `cli.py::_run_session` | C8, U3 |
| `behavior sandbox restarted under the agent` | `runners/docker.py::DockerRunner.launch` | H15 |
| `behavior agent args on create` | `cli.py::create` | U1 |
| `behavior warnings in --json payloads` | `cli.py::_emit_result` | U1, R37, H12 |
| `behavior worktree identity and port block` | `models.py::worktree_id`, `ports.py::reserve`, `ports.py::release` | B1, B15, G14 |
| `behavior placeholder names flatten` | `models.py::derive_name` | B3 |
| `behavior branch .gitmodules and dry-run submodule plan` | `provision.py::plan_submodules`, `provision.py::copy_submodules` | B28 |
| `behavior pre-push guard` | `provision.py::create` | B13 |
| `behavior progress and color off a terminal` | `output.py::Reporter` | B14 |
| `behavior .env refresh on enter` | `provision.py::enter` | C2 |
| `behavior deps re-sync on manifest change` | `provision.py::enter`, `ecosystems.py::lockfile_hash` | C3 |
| `behavior ref resolution` | `resolve.py::resolve_ref` | C4, C5 |
| `behavior agent args passthrough` | `harnesses.py::Harness.launch_argv` | C6, C8 |
| `behavior recorded creation-time choices` | `cli.py::_reconcile_with_record`, `record.py::load` | C7, C10, G5, H4 |
| `behavior host-owned record` | `record.py::WorktreeRecord`, `record.py::save` | C13, C9, C16 |
| `behavior setup model` | `lifecycle.py::SetupResult`, `lifecycle.py::setup_reason` | C14, C17 |
| `behavior worktree health` | `provision.py::worktree_health` | D2, C15, C16 |
| `behavior corrupt worktree teardown` | `cli.py::teardown`, `cleanup.py::cleanup_one` | E7 |
| `behavior unattended teardown confirmation` | `cli.py::teardown`, `cli.py::_interactive` | E1, U2 |
| `behavior cancellation` | `system.py::run_agent`, `runners/docker.py::DockerRunner._signal_agent` | U4, H17 |
| `behavior teardown confirmation prompt` | `cli.py::teardown` | I1, I2 |
| `behavior teardown chooser` | `cli.py::_choose_worktrees`, `cli.py::_prompt_selection` | I3, I4, I5 |
| `behavior chooser pull-request badges` | `forge.py::detect`, `status.py::compute` | R46 |
| `behavior parallel dispatch` | `locking.py::repo_lock`, `git.py::worktree_add` | P1, P2 |
| `behavior worktree lock` | `locking.py::worktree_lock` | P3 |
| `behavior sandbox config outside the mount` | `runners/docker.py::DockerRunner._write_config` | H2 |
| `behavior staged credential copies` | `harnesses.py::Harness.stage_credentials` | H2, R44 |
| `behavior stopped container revived` | `runners/docker.py::DockerRunner.prepare_entry` | H5 |
| `behavior firewall on every entry` | `runners/docker.py::DockerRunner.prepare_entry`, `runners/docker.py::DockerRunner.setup` | H14, H18 |
| `behavior resource limits and drift` | `runners/docker.py::DockerRunner.check_resources`, `cli.py::_check_entry_resources` | H12, G16 |
| `behavior bundled sandbox image` | `assets.py::template_dir` | H18 |
| `behavior bundled firewall host ports` | `assets.py::template_dir` | H18 |
| `behavior in-container git` | `runners/docker.py::DockerRunner._inject` | H8 |
| `behavior cleanup model` | `cleanup.py::cleanup_one`, `runners/docker.py::DockerRunner.teardown` | H6, H10 |
| `behavior .env or .gitmodules directory` | `provision.py::copy_env` | R37 |
| `behavior stray directory hint` | `provision.py::SlugConflictError` | R38 |
| `behavior nested worktree roots` | `models.py::Worktree.at`, `resolve.py::Candidate.registered` | R43 |
| `behavior submodule copy` | `submodule_copy.py::copy_submodule` | R45, R45b |
| `behavior stream discipline` | `cli.py::_emit_json`, `cli.py::_die` | X1 |
| `behavior exit-code table` | `cli.py::_classify` | X2 |
| `behavior schemaVersion` | `cli.py::SCHEMA_VERSION` | X3 |
| `behavior lock released on exit` | `locking.py::worktree_lock` | X4 |
| `behavior template init all-or-nothing` | `cli.py::template_init` | F9 |
| `behavior no host pollution` | `config.py::treebox_home` | X5 |
