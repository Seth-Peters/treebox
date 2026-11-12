# shellcheck shell=bash
# Domains allowed through the container firewall (sourced by init-firewall.sh).
# shellcheck disable=SC2034  # consumed by init-firewall.sh after sourcing
ALLOWED_DOMAINS=(
  api.anthropic.com
  sentry.io
  statsig.anthropic.com
  statsig.com

  api.openai.com
  chatgpt.com
  auth.openai.com
  cdn.openai.com

  pypi.org
  files.pythonhosted.org
  pypi.python.org
  astral.sh

  registry.npmjs.org
  nodejs.org
  deb.nodesource.com

  cdn.playwright.dev
)

# TCP ports the sandbox may reach on the Docker host (the container's default
# gateway). Empty: no host service is reachable, even one bound to 0.0.0.0.
# Open only what the agent needs, for example: ALLOWED_HOST_PORTS=(5432 8080)
ALLOWED_HOST_PORTS=()
