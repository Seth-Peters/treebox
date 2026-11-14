# Treebox routes

One row per user-visible route: the route, the feature file(s) in
`../features/` that explain how to drive it, where it lives in the source, and
the scenarios that drive it. The scenario text (command, pass criterion) lives
only in `scenarios.md`; the checks live only in `tests/e2e/scenarios.py`.

The Feature column names feature files without `.md`; a route can belong to
more than one. Source references are `path::symbol` under `src/treebox/`
(`Class.method` for a method). `tests/test_e2e_map.py` (part of `validate.sh`)
fails when:

- a CLI command or option, a `--json` error code (the collector that also
  keeps the error-code table in `docs/usage.md` complete, in
  `tests/test_contract.py`), a config key, or an environment variable that the
  source reads or a `TREEBOX_*` name it exports has no row here, or a row
  names one that no longer exists;
- a source reference names a file or symbol that does not exist;
- a row names a scenario id that `scenarios.md` or `tests/e2e/scenarios.py`
  does not have, or a scenario is in one of them but not the other, or no row
  names it;
- a row names no feature, or a feature with no file in `../features/`; a
  feature file is not linked from `../features/README.md` or no row names it;
  or a link in the feature map does not resolve.

**Adding a route** is one row here (with its feature) plus one scenario (prose
in `scenarios.md`, checks with the same id in `tests/e2e/scenarios.py`), and a
bullet in the feature file when a user drives it differently from what the
file already shows. A `behavior …` row is free-form: use it for a route that
is not a flag, an error code, a config key or an environment variable.

## Commands and options

| Route | Feature | Source | Scenarios |
| --- | --- | --- | --- |
| `--version` | doctor | `cli.py::_version_callback` | A1 |
| `--non-interactive` | unattended | `cli.py::main`, `cli.py::_interactive` | U2, B25 |
| `--install-completion` | doctor | `cli.py::app` | A7 |
| `--show-completion` | doctor | `cli.py::app` | A7 |
| `create` | create | `cli.py::create`, `provision.py::create` | B1, B2, B3, B7, B8 |
| `create --checkout` | create | `cli.py::create`, `provision.py::create` | B5, B17, R37 |
| `create --repo` | create | `cli.py::_repo_root` | B1, B19 |
| `create --root` | create | `models.py::worktree_root` | B1, B6 |
| `create --base` | create | `provision.py::create` | B4, G4 |
| `create --isolation` | create, docker-sandbox | `runners/__init__.py::get_runner` | H1, B22 |
| `create --harness` | create | `harnesses.py::get_harness` | B12 |
| `create --cold` | create | `runners/host.py::HostRunner.setup` | B24, R39 |
| `create --no-fetch` | create | `git.py::fetch_origin` | B9 |
| `create --firewall` | firewall | `runners/docker.py::DockerRunner._overlaid_config` | G10 |
| `create --template` | template, docker-sandbox | `assets.py::template_dir` | H1, H3, B21 |
| `create --cpus` | resource-limits | `cli.py::_creation_limits`, `resources.py::parse_cpus`, `runners/docker.py::DockerRunner.check_resources` | B26, B23, H12 |
| `create --memory` | resource-limits | `cli.py::_creation_limits`, `resources.py::parse_memory` | B26, H12 |
| `create --pids-limit` | resource-limits | `cli.py::_creation_limits`, `resources.py::parse_pids` | B26, H12 |
| `create --dry-run` | create | `cli.py::_dry_run`, `provision.py::dry_run_plan` | B10, B10b, R39 |
| `create --print` | create | `cli.py::_emit_result` | B11 |
| `create --json` | create | `cli.py::_emit_result` | B1 |
| `create --quiet` | create | `output.py::Reporter` | B14 |
| `create --verbose` | create | `output.py::Reporter` | B14 |
| `enter` | enter | `cli.py::enter`, `provision.py::enter` | C1, C8, C18 |
| `enter --repo` | enter | `cli.py::_repo_root` | C1 |
| `enter --root` | enter | `resolve.py::resolve_ref` | C1, R43 |
| `enter --isolation` | enter, docker-sandbox | `cli.py::_reconcile_with_record` | C7, H4 |
| `enter --harness` | enter | `cli.py::_reconcile_with_record` | C10 |
| `enter --template` | enter, template | `cli.py::_reconcile_with_record` | H11 |
| `enter --cpus` | resource-limits | `cli.py::_check_entry_resources` | H12 |
| `enter --memory` | resource-limits | `cli.py::_check_entry_resources` | H12 |
| `enter --pids-limit` | resource-limits | `cli.py::_check_entry_resources` | H12 |
| `enter --cold` | enter | `provision.py::enter` | C11 |
| `enter --print` | enter | `cli.py::_emit_result` | C1, C6 |
| `enter --json` | enter | `cli.py::_emit_result` | C4 |
| `enter --quiet` | enter | `output.py::Reporter` | C12 |
| `enter --verbose` | enter | `output.py::Reporter` | C12 |
| `list` | list | `cli.py::list_cmd`, `cli.py::_collect_rows` | D1, D2 |
| `list --repo` | list | `cli.py::_repo_root` | D1 |
| `list --root` | list | `models.py::worktree_root` | D3 |
| `list --json` | list | `cli.py::list_cmd` | D2 |
| `ls` | list | `cli.py::list_cmd` | D4 |
| `teardown` | teardown | `cli.py::teardown`, `cleanup.py::cleanup_one` | E2, E5, E6, C18 |
| `teardown --repo` | teardown | `cli.py::_repo_root` | E1 |
| `teardown --root` | teardown | `resolve.py::resolve_ref` | R43 |
| `teardown --yes` | teardown | `cli.py::teardown` | E1 |
| `teardown --delete-branch` | teardown | `cli.py::_teardown_one` | E4, I5 |
| `teardown --remove-volumes` | teardown, docker-sandbox | `runners/docker.py::DockerRunner.teardown` | H6 |
| `teardown --force` | teardown | `cli.py::teardown` | E2, E3, E7 |
| `teardown --skip-container` | teardown, docker-sandbox | `cli.py::_teardown_one` | H6 |
| `teardown --json` | teardown | `cli.py::TeardownRecord` | E4 |
| `teardown --quiet` | teardown | `output.py::Reporter` | E11 |
| `teardown --verbose` | teardown | `output.py::Reporter` | E11 |
| `rm` | teardown | `cli.py::teardown` | E8 |
| `doctor` | doctor | `cli.py::doctor` | A2 |
| `doctor --repo` | doctor | `cli.py::doctor` | A2 |
| `doctor --isolation` | doctor | `cli.py::doctor` | A4, A5, B23 |
| `doctor --harness` | doctor | `cli.py::doctor`, `cli.py::_agent_binary_check` | A9 |
| `doctor --json` | doctor | `cli.py::doctor` | A3 |
| `version` | doctor | `cli.py::version` | A1 |
| `template` | template | `cli.py::template_app` | A6, A8 |
| `template init` | template | `cli.py::template_init` | F3 |
| `template init --from` | template | `cli.py::template_init` | F5, F6 |
| `template init --force` | template | `cli.py::template_init` | F4, F9 |
| `template init --json` | template | `cli.py::template_init` | F3 |
| `template list` | template | `cli.py::template_list` | F1 |
| `template list --json` | template | `cli.py::template_list` | F1 |
| `template ls` | template | `cli.py::template_list` | F7 |
| `template path` | template | `cli.py::template_path` | F2 |

## Error codes (`--json` `error.code`)

| Route | Feature | Source | Scenarios |
| --- | --- | --- | --- |
| `error AMBIGUOUS_REF` | enter | `resolve.py::AmbiguousRefError` | C5 |
| `error BRANCH_EXISTS` | create | `provision.py::BranchConflictError` | B6, B10b |
| `error BRANCH_IN_USE` | create | `provision.py::BranchInUseError` | B17, B10b |
| `error CANCELLED` | unattended, remote-workers | `cli.py::_run_session`, `system.py::run_agent` | U4, H17 |
| `error COPY_FAILED` | template | `cli.py::template_init` | F9 |
| `error DESTINATION_CONFLICT` | create | `provision.py::DestinationConflictError` | R45 |
| `error DIRTY_WORKTREE` | teardown | `cli.py::teardown` | E2 |
| `error DOCKER_UNAVAILABLE` | docker-sandbox | `runners/docker.py::DockerRunner.preflight` | B23 |
| `error ERROR` | unattended | `cli.py::_classify` | B16 |
| `error FETCH_FAILED` | create | `git.py::FetchError` | B9 |
| `error FIREWALL_FAILED` | firewall | `runners/base.py::FirewallError`, `runners/docker.py::DockerRunner.prepare_entry` | H14 |
| `error GIT_MISSING` | doctor, list | `git.py::GitMissingError` | B20 |
| `error HARNESS_MISSING` | unattended, docker-sandbox | `cli.py::_require_agent`, `runners/docker.py::DockerRunner.launch` | B16, H16 |
| `error INVALID_BRANCH` | create | `cli.py::_validate_branch` | B18 |
| `error INVALID_CONFIG` | config | `cli.py::_resolve_config`, `config.py::load_config` | G1, G3, G12, G16 |
| `error INVALID_LINKAGE` | enter, list | `provision.py::InvalidLinkageError` | C15 |
| `error INVALID_NAME` | create, template | `cli.py::_validate_name`, `cli.py::template_init` | B8, F8 |
| `error INVALID_RESOURCE` | resource-limits | `resources.py::ResourceLimitError`, `cli.py::_resource_flags`, `cli.py::_run_session` | B26 |
| `error INVALID_ROOT` | create | `cli.py::_check_root` | B27 |
| `error ISOLATION_MISMATCH` | enter | `cli.py::_reconcile_with_record` | C7 |
| `error LOCK_HELD` | create, remote-workers | `locking.py::worktree_lock` | P3 |
| `error LOGIN_MISSING` | unattended | `cli.py::_require_agent`, `cli.py::_agent_login_check` | B25 |
| `error MAIN_WORKTREE` | teardown | `cleanup.py::MainWorktreeError` | E9 |
| `error MISSING_DEPENDENCY` | docker-sandbox | `runners/base.py::PreflightError` | B22, B26 |
| `error NEEDS_CONFIRMATION` | teardown, unattended | `cli.py::teardown` | E1, E7, U2 |
| `error NOT_A_REPO` | create | `cli.py::_repo_root` | B19 |
| `error NOT_FOUND` | enter, create, remote-workers | `provision.py::NotFoundError` | C5, B10b, C18 |
| `error OVERWRITE_FAILED` | template | `cli.py::template_init` | F9 |
| `error PORTS_EXHAUSTED` | identity-and-ports | `ports.py::PortsExhaustedError` | G14 |
| `error PORT_LEDGER_INVALID` | identity-and-ports | `ports.py::LedgerInvalidError` | G15 |
| `error RECORD_CONFLICT` | create | `provision.py::RecordConflictError` | C17 |
| `error RECORD_INVALID` | enter | `record.py::RecordInvalidError` | C9 |
| `error RECORD_MISSING` | enter | `record.py::RecordMissingError` | C16 |
| `error RESOURCE_MISMATCH` | resource-limits | `cli.py::_check_entry_resources` | H12 |
| `error RESOURCE_UNSUPPORTED` | resource-limits | `cli.py::_refuse_unsupported_limits` | B26 |
| `error RUNNING_AS_ROOT` | docker-sandbox | `runners/docker.py::DockerRunner.preflight` | B29 |
| `error SETUP_FAILED` | create, enter | `provision.py::SetupFailedError` | C14, C17 |
| `error SLUG_CONFLICT` | create | `provision.py::SlugConflictError` | B7, R38 |
| `error SUBMODULE_COPY_FAILED` | create | `provision.py::SubmoduleCopyFailedError` | R45b |
| `error TEARDOWN_INCOMPLETE` | teardown, docker-sandbox, remote-workers | `cleanup.py::CleanupResult`, `cli.py::teardown` | H10 |
| `error TEMPLATE_CONFLICT` | template | `cli.py::template_init` | F8 |
| `error TEMPLATE_EXISTS` | template | `cli.py::template_init` | F4 |
| `error TEMPLATE_INVALID` | template | `assets.py::TemplateInvalidError` | B21 |
| `error TEMPLATE_NOT_FOUND` | template | `assets.py::TemplateNotFoundError` | F6, H3, G11 |
| `error WORKTREE_LOCKED` | teardown | `cli.py::teardown` | E10 |

## Config keys (`config.toml`)

| Route | Feature | Source | Scenarios |
| --- | --- | --- | --- |
| `config isolation` | config, docker-sandbox | `config.py::Config` | G3 |
| `config harness` | config | `config.py::Config` | G5 |
| `config base` | config | `config.py::Config` | G2, G4 |
| `config root` | config | `config.py::Config`, `models.py::worktree_root` | G6 |
| `config env_file` | config | `config.py::Config`, `provision.py::resolve_env_file` | G7 |
| `config firewall` | config, firewall | `config.py::Config` | G10 |
| `config template` | config, template | `config.py::Config`, `assets.py::template_dir` | G11 |
| `config setup_hook` | config | `config.py::Config`, `runners/host.py::HostRunner._run_override` | G8 |
| `config caches` | config | `config.py::default_caches` | G9 |
| `config resources` | config, resource-limits | `config.py::Config`, `resources.py::from_mapping` | G16, H12 |
| `config ports` | config, identity-and-ports | `config.py::PortRange`, `ports.py::reserve` | G14 |

## Environment variables

| Route | Feature | Source | Scenarios |
| --- | --- | --- | --- |
| `env TREEBOX_HOME` | config, template | `config.py::treebox_home` | F3 |
| `env TREEBOX_CONFIG` | config | `config.py::config_path` | G1 |
| `env TREEBOX_TEMPLATE_DIR` | template | `assets.py::template_dir` | G13 |
| `env XDG_CACHE_HOME` | config | `config.py::default_caches` | G9 |
| `env TZ` | docker-sandbox | `runners/docker.py::DockerRunner._inject` | G13 |
| `env TREEBOX_NON_INTERACTIVE` | unattended | `cli.py::main` | U2, B25 |
| `env TREEBOX_NAME` | identity-and-ports | `models.py::Worktree.treebox_env` | B15, H7 |
| `env TREEBOX_ID` | identity-and-ports | `models.py::worktree_id`, `models.py::Worktree.treebox_env` | B1, B15, H7 |
| `env TREEBOX_PORT_BASE` | identity-and-ports | `ports.py::reserve`, `models.py::Worktree.treebox_env` | B1, B15, G14, H7 |
| `env TREEBOX_PORT_COUNT` | identity-and-ports | `models.py::Worktree.treebox_env` | B15 |

## Behaviors

| Route | Feature | Source | Scenarios |
| --- | --- | --- | --- |
| `behavior help and bare invocation` | doctor | `cli.py::app` | A8 |
| `behavior shell completion protocol` | doctor | `cli.py::app` | A6 |
| `behavior doctor checks` | doctor | `cli.py::doctor`, `cli.py::_doctor_problems` | A2, A3, A4, A9 |
| `behavior macOS Keychain login` | doctor, docker-sandbox | `harnesses.py::Harness.login`, `harnesses.py::Harness.stage_credentials` | A10, H13 |
| `behavior real launch on the host` | create | `runners/host.py::HostRunner.launch` | B15, B16, C8 |
| `behavior real launch in a container` | docker-sandbox | `runners/docker.py::DockerRunner.launch` | H7, H16 |
| `behavior agent exit line` | unattended, remote-workers | `cli.py::AGENT_EXIT_PREFIX`, `cli.py::_run_session` | C8, U3 |
| `behavior sandbox restarted under the agent` | docker-sandbox, remote-workers | `runners/docker.py::DockerRunner.launch` | H15 |
| `behavior agent args on create` | create | `cli.py::create` | U1 |
| `behavior warnings in --json payloads` | unattended | `cli.py::_emit_result` | U1, R37, H12 |
| `behavior worktree identity and port block` | identity-and-ports, remote-workers | `models.py::worktree_id`, `ports.py::reserve`, `ports.py::release` | B1, B15, G14 |
| `behavior placeholder names flatten` | create | `models.py::derive_name` | B3 |
| `behavior branch .gitmodules and dry-run submodule plan` | create | `provision.py::plan_submodules`, `provision.py::copy_submodules` | B28 |
| `behavior pre-push guard` | create | `provision.py::create` | B13 |
| `behavior progress and color off a terminal` | create | `output.py::Reporter` | B14 |
| `behavior .env refresh on enter` | enter | `provision.py::enter` | C2 |
| `behavior deps re-sync on manifest change` | enter | `provision.py::enter`, `ecosystems.py::lockfile_hash` | C3 |
| `behavior ref resolution` | enter, remote-workers | `resolve.py::resolve_ref` | C4, C5 |
| `behavior agent args passthrough` | enter | `harnesses.py::Harness.launch_argv` | C6, C8 |
| `behavior recorded creation-time choices` | enter | `cli.py::_reconcile_with_record`, `record.py::load` | C7, C10, G5, H4 |
| `behavior host-owned record` | enter | `record.py::WorktreeRecord`, `record.py::save` | C13, C9, C16 |
| `behavior setup model` | create, enter | `lifecycle.py::SetupResult`, `lifecycle.py::setup_reason` | C14, C17 |
| `behavior worktree health` | list | `provision.py::worktree_health` | D2, C15, C16 |
| `behavior corrupt worktree teardown` | teardown | `cli.py::teardown`, `cleanup.py::cleanup_one` | E7 |
| `behavior unattended teardown confirmation` | teardown, unattended | `cli.py::teardown`, `cli.py::_interactive` | E1, U2 |
| `behavior cancellation` | unattended, remote-workers | `system.py::run_agent`, `runners/docker.py::DockerRunner._signal_agent` | U4, H17 |
| `behavior teardown confirmation prompt` | teardown | `cli.py::teardown` | I1, I2 |
| `behavior teardown chooser` | teardown | `cli.py::_choose_worktrees`, `cli.py::_prompt_selection` | I3, I4, I5 |
| `behavior chooser pull-request badges` | teardown | `forge.py::detect`, `status.py::compute` | R46 |
| `behavior parallel dispatch` | create, remote-workers | `locking.py::repo_lock`, `git.py::worktree_add` | P1, P2 |
| `behavior worktree lock` | create | `locking.py::worktree_lock` | P3 |
| `behavior sandbox config outside the mount` | docker-sandbox | `runners/docker.py::DockerRunner._write_config` | H2 |
| `behavior staged credential copies` | docker-sandbox | `harnesses.py::Harness.stage_credentials` | H2, R44 |
| `behavior stopped container revived` | docker-sandbox | `runners/docker.py::DockerRunner.prepare_entry` | H5 |
| `behavior removed container re-created on enter` | docker-sandbox | `runners/docker.py::DockerRunner.setup_reason`, `provision.py::enter` | H9 |
| `behavior firewall on every entry` | firewall | `runners/docker.py::DockerRunner.prepare_entry`, `runners/docker.py::DockerRunner.setup` | H14, H18 |
| `behavior resource limits and drift` | resource-limits | `runners/docker.py::DockerRunner.check_resources`, `cli.py::_check_entry_resources` | H12, G16 |
| `behavior bundled sandbox image` | docker-sandbox, template | `assets.py::template_dir` | H18 |
| `behavior bundled firewall host ports` | firewall | `assets.py::template_dir` | H18 |
| `behavior in-container git` | docker-sandbox | `runners/docker.py::DockerRunner._inject` | H8 |
| `behavior cleanup model` | teardown, docker-sandbox | `cleanup.py::cleanup_one`, `runners/docker.py::DockerRunner.teardown` | H6, H9, H10 |
| `behavior .env or .gitmodules directory` | create | `provision.py::copy_env` | R37 |
| `behavior stray directory hint` | create | `provision.py::SlugConflictError` | R38 |
| `behavior nested worktree roots` | enter, teardown | `models.py::Worktree.at`, `resolve.py::Candidate.registered` | R43 |
| `behavior submodule copy` | create | `submodule_copy.py::copy_submodule` | R45, R45b |
| `behavior stream discipline` | unattended | `cli.py::_emit_json`, `cli.py::_die` | X1 |
| `behavior exit-code table` | unattended, remote-workers | `cli.py::_classify` | X2 |
| `behavior schemaVersion` | unattended | `cli.py::SCHEMA_VERSION` | X3 |
| `behavior lock released on exit` | create | `locking.py::worktree_lock` | X4 |
| `behavior template init all-or-nothing` | template | `cli.py::template_init` | F9 |
| `behavior no host pollution` | config | `config.py::treebox_home` | X5 |
