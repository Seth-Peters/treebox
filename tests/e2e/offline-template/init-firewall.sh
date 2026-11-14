#!/bin/sh
# The offline e2e template has no egress firewall: building one needs iptables
# and network lookups. It ships no firewall.json, so `treebox template list`
# reports firewall support as absent; this file exists only because every
# template must contain it. Fail loudly if it is ever run.
echo "treebox e2e offline template: no firewall support" >&2
exit 1
