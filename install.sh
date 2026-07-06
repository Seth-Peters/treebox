#!/bin/sh
# treebox installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Seth-Peters/treebox/main/install.sh | sh
#
# It ensures uv (https://docs.astral.sh/uv/) is available, then installs treebox
# as an isolated uv tool. uv supplies a managed Python if the host's is too old,
# so the one-liner works even where the system Python predates treebox's floor.
#
# If uv is missing, the script fails loudly by default — it won't install a
# package manager behind your back. Pass TREEBOX_AUTO_UV=1 to opt into that.
#
# Environment overrides:
#   TREEBOX_INSTALL_SPEC  What to install. A PyPI spec ("treebox", "treebox==0.4.0")
#                         or a "git+https://..." URL. Default: "treebox" (PyPI).
#   TREEBOX_TESTPYPI=1    Resolve treebox from TestPyPI (deps still from PyPI).
#   TREEBOX_AUTO_UV=1     Auto-install uv if it's missing (default: fail loudly).
set -eu

# Install from PyPI by default. A user-set TREEBOX_INSTALL_SPEC always wins —
# pass a "git+https://..." spec to install straight from the repo instead.
SPEC="${TREEBOX_INSTALL_SPEC:-treebox}"

say() { printf '==> %s\n' "$1"; }
err() { printf 'error: %s\n' "$1" >&2; }

# --- ensure uv -------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  if [ "${TREEBOX_AUTO_UV:-}" != "1" ]; then
    err "uv is required but not installed."
    err "Install it: https://docs.astral.sh/uv/getting-started/installation/"
    err "Or re-run with TREEBOX_AUTO_UV=1 to let this script install uv for you."
    exit 1
  fi
  say "uv not found — installing it (TREEBOX_AUTO_UV=1)"
  curl -fsSL https://astral.sh/uv/install.sh | sh
  # uv lands in ~/.local/bin by default; make it usable for the rest of this run.
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  err "uv still isn't on PATH after install. Open a new shell and re-run."
  exit 1
fi

# --- install treebox -------------------------------------------------------
set -- --force "$SPEC"
if [ "${TREEBOX_TESTPYPI:-}" = "1" ]; then
  # treebox lives on TestPyPI; its dependencies (typer, ...) do not — so search
  # both indexes and let uv pick the best match across them.
  set -- --index "https://test.pypi.org/simple/" \
         --index "https://pypi.org/simple/" \
         --index-strategy unsafe-best-match \
         "$@"
fi

say "Installing treebox ($SPEC)"
uv tool install "$@"

# --- PATH check ------------------------------------------------------------
if command -v treebox >/dev/null 2>&1; then
  say "Installed. Try: treebox doctor"
else
  say "Installed, but 'treebox' isn't on your PATH yet."
  echo "    Run 'uv tool update-shell' (or add ~/.local/bin to PATH), then restart your shell."
fi
