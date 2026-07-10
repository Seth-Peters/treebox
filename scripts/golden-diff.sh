#!/usr/bin/env bash
# Golden output snapshots for the CLI's observable surface.
#
# Runs a fixed matrix of commands — doctor, create --print/--dry-run,
# enter --print, and their --json forms, for both isolations — against a
# hermetic throwaway repo, normalizes the machine-specific bits (paths,
# uid/gid, git version, container-name digests), and diffs the result against
# the committed snapshots in tests/golden/. Behavior-preserving changes must
# leave every snapshot byte-identical: the "output identical" gate is
# mechanical, not eyeballed.
#
#   scripts/golden-diff.sh            # compare against tests/golden/ (CI gate)
#   scripts/golden-diff.sh --update   # regenerate the snapshots (review the diff!)
#
# Docker is replaced by a no-op shim on PATH so the docker-isolation cases are
# deterministic and run on hosts without a daemon: `docker ps -aq` reports no
# containers and every other subcommand succeeds silently, which drives the
# fresh-create path (render config -> stage credentials -> build -> run ->
# post-create) end to end without a real container.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MODE="diff"
[[ "${1:-}" == "--update" ]] && MODE="update"

GOLDEN_DIR="tests/golden"
ROOT_PARENT="${TREEBOX_GOLDEN_ROOT:-/tmp}"
ROOT_PARENT="${ROOT_PARENT%/}"
[[ -n "$ROOT_PARENT" ]] || { echo "FATAL: TREEBOX_GOLDEN_ROOT must not be /" >&2; exit 1; }
mkdir -p "$ROOT_PARENT"
ROOT="$(mktemp -d "$ROOT_PARENT/treebox-golden.XXXXXX")"
ROOT="$(cd "$ROOT" && pwd -P)"
WORK="$ROOT/out"
trap 'rm -rf "$ROOT"' EXIT

TREEBOX="$PWD/.venv/bin/treebox"
[[ -x "$TREEBOX" ]] || uv sync --quiet
[[ -x "$TREEBOX" ]] || { echo "FATAL: $TREEBOX not found after uv sync" >&2; exit 1; }

mkdir -p "$WORK" "$ROOT/tbhome" "$ROOT/home-empty"

# --- fake HOME with both subscription logins staged --------------------------
HOME_FAKE="$ROOT/home"
mkdir -p "$HOME_FAKE/.claude" "$HOME_FAKE/.codex"
printf '{"token": "golden"}\n' > "$HOME_FAKE/.claude/.credentials.json"
printf '{}\n' > "$HOME_FAKE/.claude/settings.json"
printf '{}\n' > "$HOME_FAKE/.codex/auth.json"
printf '\n' > "$HOME_FAKE/.codex/config.toml"

# --- docker shim: no containers exist; every subcommand succeeds -------------
SHIM="$ROOT/shim"
mkdir -p "$SHIM"
printf '#!/bin/sh\nexit 0\n' > "$SHIM/docker"
chmod +x "$SHIM/docker"

# --- hermetic config: setup is a marker-writing hook, never a real installer -
CFG="$ROOT/config.toml"
printf 'setup_hook = ["echo ran >> setup.log"]\n' > "$CFG"

# --- fixture repo: a working clone of a local bare origin --------------------
git_q() { git -c init.defaultBranch=main "$@" >/dev/null; }
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@e GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@e
git_q init --bare -b main "$ROOT/origin.git"
git_q clone "$ROOT/origin.git" "$ROOT/repo" 2>/dev/null
REPO="$ROOT/repo"
printf '[project]\nname="golden"\nversion="0"\n' > "$REPO/pyproject.toml"
printf 'version = 1\n' > "$REPO/uv.lock"
printf 'SECRET=golden\n' > "$REPO/.env"
git_q -C "$REPO" add -A
git_q -C "$REPO" commit -qm init
git_q -C "$REPO" push -q origin main

# Local-only repo with no .env: doctor should report the optional miss as a
# muted note, not a hard failure row. Keep it remote-free so git auth stays
# deterministic.
REPO_NO_ENV="$ROOT/repo-no-env"
git_q init -b main "$REPO_NO_ENV"
printf "[project]\nname=\"golden-no-env\"\nversion=\"0\"\n" > "$REPO_NO_ENV/pyproject.toml"
git_q -C "$REPO_NO_ENV" add -A
git_q -C "$REPO_NO_ENV" commit -qm init

WTS="$ROOT/wts"

GITVER="$(git --version | awk '{print $3}')"
GITVER_PARSED="$(git --version | perl -lane 'my $s = $F[-1]; my @parts = split /\./, $s; my @nums; for my $p (@parts[0..2]) { $p //= ""; $p =~ s/\D//g; push @nums, length($p) ? $p : 0; } push @nums, 0 while @nums < 3; print join ".", @nums[0..2]')"
export GD_ROOT="$ROOT" GD_PRIVROOT="/private$ROOT" GD_GITVER="$GITVER" GD_GITVER_PARSED="$GITVER_PARSED"
# Literal (not pattern) uid/gid substitutions, so a regression that changed
# one number into another is a snapshot diff, not silently normalized away.
GD_UID="$(id -u)"
GD_GID="$(id -g)"
export GD_UID GD_GID

normalize() {
  perl -pe '
    s/\Q$ENV{GD_PRIVROOT}\E/__ROOT__/g;
    s/\Q$ENV{GD_ROOT}\E/__ROOT__/g;
    s/\Q$ENV{GD_GITVER_PARSED}\E/__GITVER__/g;
    s/\Q$ENV{GD_GITVER}\E/__GITVER__/g;
    s/\bUSER_UID=\Q$ENV{GD_UID}\E\b/USER_UID=__UID__/g;
    s/\bUSER_GID=\Q$ENV{GD_GID}\E\b/USER_GID=__GID__/g;
    s/\b\Q$ENV{GD_UID}\E:\Q$ENV{GD_GID}\E\b/__UID__:__GID__/g;
    s/\btreebox-([a-z0-9._-]+)-[0-9a-f]{10}\b/treebox-$1-__HASH__/g;
  '
}

FAILED=0

# run_case <name> <home-dir> <treebox args...>
run_case() {
  local name="$1" home="$2"
  shift 2
  local rc=0
  set +e
  env -u TZ -u XDG_CACHE_HOME -u PNPM_HOME -u UV_CACHE_DIR -u npm_config_cache \
      -u npm_config_store_dir -u GOMODCACHE -u CARGO_HOME -u TREEBOX_TEMPLATE_DIR \
      -u NO_COLOR -u FORCE_COLOR -u CLICOLOR_FORCE \
      HOME="$home" TREEBOX_CONFIG="$CFG" TREEBOX_HOME="$ROOT/tbhome" \
      PATH="$SHIM:$PATH" COLUMNS=80 \
      "$TREEBOX" "$@" >"$WORK/$name.stdout" 2>"$WORK/$name.stderr"
  rc=$?
  set -e
  {
    echo "# treebox $*" | normalize
    echo "# exit: $rc"
    echo "# --- stdout ---"
    normalize < "$WORK/$name.stdout"
    echo "# --- stderr ---"
    normalize < "$WORK/$name.stderr"
  } > "$WORK/$name.txt"
  rm -f "$WORK/$name.stdout" "$WORK/$name.stderr"

  if [[ "$MODE" == "update" ]]; then
    cp "$WORK/$name.txt" "$GOLDEN_DIR/$name.txt"
    echo "updated  $name"
  elif [[ ! -f "$GOLDEN_DIR/$name.txt" ]]; then
    echo "MISSING snapshot: $GOLDEN_DIR/$name.txt (run with --update on main first)" >&2
    FAILED=1
  elif ! diff -u "$GOLDEN_DIR/$name.txt" "$WORK/$name.txt"; then
    echo "DIFF     $name" >&2
    FAILED=1
  else
    echo "ok       $name"
  fi
}

mkdir -p "$GOLDEN_DIR"
R=(--repo "$REPO")
RW=(--repo "$REPO" --root "$WTS")

# --- host isolation -----------------------------------------------------------
run_case doctor-host          "$HOME_FAKE" doctor "${R[@]}"
run_case doctor-host-json     "$HOME_FAKE" doctor "${R[@]}" --json
run_case doctor-host-no-env   "$HOME_FAKE" doctor --repo "$REPO_NO_ENV"
run_case doctor-no-login      "$ROOT/home-empty" doctor "${R[@]}"
run_case create-dryrun-host       "$HOME_FAKE" create golden-host "${RW[@]}" --dry-run
run_case create-dryrun-host-json  "$HOME_FAKE" create golden-host "${RW[@]}" --dry-run --json
run_case create-host-print    "$HOME_FAKE" create golden-host "${RW[@]}" --print
run_case create-host-json     "$HOME_FAKE" create golden-host-json "${RW[@]}" --json
# A slash name: the branch keeps the slash, the directory flattens it to --.
run_case create-slash-json    "$HOME_FAKE" create golden/slash "${RW[@]}" --json
run_case enter-host-print     "$HOME_FAKE" enter golden-host "${RW[@]}" --print
run_case enter-host-json      "$HOME_FAKE" enter golden-host "${RW[@]}" --json

# --- docker isolation (through the shim) ---------------------------------------
run_case doctor-docker        "$HOME_FAKE" doctor "${R[@]}" --isolation docker
run_case doctor-docker-json   "$HOME_FAKE" doctor "${R[@]}" --isolation docker --json
run_case create-dryrun-docker      "$HOME_FAKE" create golden-docker "${RW[@]}" --isolation docker --dry-run
run_case create-dryrun-docker-json "$HOME_FAKE" create golden-docker "${RW[@]}" --isolation docker --dry-run --json
run_case create-docker-print  "$HOME_FAKE" create golden-docker "${RW[@]}" --isolation docker --print
run_case create-docker-json   "$HOME_FAKE" create golden-docker-json "${RW[@]}" --isolation docker --json
run_case enter-docker-print   "$HOME_FAKE" enter golden-docker "${RW[@]}" --print
run_case enter-docker-json    "$HOME_FAKE" enter golden-docker "${RW[@]}" --json

if [[ "$MODE" == "diff" && "$FAILED" -ne 0 ]]; then
  echo >&2
  echo "golden-diff: OUTPUT CHANGED — the output-stability contract requires" >&2
  echo "byte-identical CLI output. Only regenerate with --update once you have" >&2
  echo "confirmed the change is intentional." >&2
  exit 1
fi
echo "golden-diff: all snapshots match"
