#!/bin/bash
set -euo pipefail

# Baked into the image at /usr/local/bin, so the workspace can't be derived from
# this script's location. The runner execs it with the workspace as the working
# dir; $1 overrides that when given.
WS="${1:-${PWD}}"

# Egress lockdown gate: when the operator enabled the firewall, refuse to run
# anything derived from the untrusted workspace (uv sync executes the repo's
# build backend) until init-firewall.sh has established default-deny egress.
# The runner execs the firewall first; this fails closed if that ordering is
# ever lost.
if [[ "${TREEBOX_FIREWALL:-}" == "1" && ! -f /run/treebox-firewall-ready ]]; then
  echo "ERROR: firewall enabled but not initialized; refusing to run workspace setup." >&2
  echo "       init-firewall.sh must complete before post-create.sh." >&2
  exit 1
fi

if [[ -f "$WS/pyproject.toml" ]] && command -v uv >/dev/null 2>&1; then
  echo "Running uv sync..."
  (cd "$WS" && uv sync)
elif [[ ! -d "$WS/.venv" ]] && { [[ -f "$WS/requirements.txt" ]] || [[ -f "$WS/setup.py" ]]; }; then
  echo "Creating Python virtual environment..."
  (cd "$WS" && uv venv .venv)
  if [[ -f "$WS/requirements.txt" ]]; then
    echo "Installing requirements.txt..."
    (cd "$WS" && uv pip install -r requirements.txt)
  fi
  if [[ -f "$WS/setup.py" ]]; then
    echo "Installing project in editable mode..."
    (cd "$WS" && uv pip install -e .)
  fi
fi

if [[ -d "$HOME/.ssh" ]]; then
  chmod 700 "$HOME/.ssh" 2>/dev/null || true
  chmod 600 "$HOME/.ssh"/* 2>/dev/null || true
  chmod 644 "$HOME/.ssh"/*.pub 2>/dev/null || true
  chmod 644 "$HOME/.ssh/known_hosts" 2>/dev/null || true
fi

if command -v playwright-cli >/dev/null 2>&1; then
  echo "Installing playwright skills..."
  (cd "$WS" && playwright-cli install --skills)
fi

echo "post-create.sh done."
