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
