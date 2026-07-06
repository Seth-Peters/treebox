#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

# post-create.sh refuses to touch the (untrusted) workspace until this exists;
# it is only ever created after a fully successful lockdown (see the end).
READY_FLAG=/run/treebox-firewall-ready

# The runner execs this BOTH at setup (so default-deny egress exists before any
# workspace-derived code runs in post-create.sh) and on every container restart
# (rules don't survive a restart). When this boot already locked down
# successfully, skip the rebuild rather than tearing live rules down
# mid-session. Requires the flag AND the live policy: policy DROP alone also
# matches the fail_closed end state, and the flag alone could be stale if /run
# survived a restart. Placed before the trap so a clean skip cannot trip it.
if [[ -f "$READY_FLAG" ]] && iptables -S OUTPUT 2>/dev/null | grep -q '^-P OUTPUT DROP'; then
  echo "Firewall already active; skipping reconfiguration."
  exit 0
fi
# A rebuild is starting: whatever a previous boot left behind, the firewall is
# not ready until this run completes.
rm -f "$READY_FLAG"

# Fail closed: if setup aborts for any reason, drop all traffic rather than
# leaving the container's inherited default-ACCEPT policies in place.
fail_closed() {
  local status=$?
  if [[ "$status" -ne 0 ]]; then
    iptables -P INPUT DROP
    iptables -P FORWARD DROP
    iptables -P OUTPUT DROP
    iptables -F
    if ip6tables -L INPUT >/dev/null 2>&1; then
      ip6tables -P INPUT DROP
      ip6tables -P FORWARD DROP
      ip6tables -P OUTPUT DROP
      ip6tables -F
    fi
    echo "ERROR: firewall setup failed; all traffic dropped" >&2
  fi
}
trap fail_closed EXIT

DOCKER_DNS_RULES=$(iptables-save -t nat | grep "127\.0\.0\.11" || true)
DNS_SERVERS=$(awk '/^nameserver/ {print $2}' /etc/resolv.conf | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || true)

# Fetch everything that needs open egress before locking down.
echo "Fetching GitHub IP ranges..."
gh_ranges=$(curl -s https://api.github.com/meta)
[[ -n "$gh_ranges" ]] || { echo "ERROR: Failed to fetch GitHub IP ranges"; exit 1; }
echo "$gh_ranges" | jq -e '.web and .api and .git' >/dev/null

DOMAINS_FILE=/usr/local/share/allowed-domains.sh
[[ -f "$DOMAINS_FILE" ]] || { echo "ERROR: $DOMAINS_FILE missing"; exit 1; }
# shellcheck source=src/treebox/assets/container/allowed-domains.sh
source "$DOMAINS_FILE"
[[ ${#ALLOWED_DOMAINS[@]} -gt 0 ]] || { echo "ERROR: ALLOWED_DOMAINS is empty"; exit 1; }

# Default-deny before any rules are rebuilt: flushing alone would leave the
# inherited ACCEPT policies in place, so an abort mid-setup would fail open.
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X
ipset destroy allowed-domains 2>/dev/null || true

# The allowlist is IPv4-only, so IPv6 would bypass it entirely: drop all
# IPv6 traffic except loopback.
if ip6tables -L INPUT >/dev/null 2>&1; then
  ip6tables -P INPUT DROP
  ip6tables -P FORWARD DROP
  ip6tables -P OUTPUT DROP
  ip6tables -F
  ip6tables -X
  ip6tables -A INPUT  -i lo -j ACCEPT
  ip6tables -A OUTPUT -o lo -j ACCEPT
else
  echo "WARN: ip6tables unavailable; assuming no IPv6 stack"
fi

if [[ -n "$DOCKER_DNS_RULES" ]]; then
  iptables -t nat -N DOCKER_OUTPUT 2>/dev/null || true
  iptables -t nat -N DOCKER_POSTROUTING 2>/dev/null || true
  echo "$DOCKER_DNS_RULES" | xargs -L 1 iptables -t nat
fi

iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# DNS only to the configured resolvers; docker's embedded resolver
# (127.0.0.11) is covered by the restored NAT rules plus the loopback allow.
while read -r resolver; do
  [[ -n "$resolver" ]] || continue
  iptables -A OUTPUT -p udp --dport 53 -d "$resolver" -j ACCEPT
  iptables -A OUTPUT -p tcp --dport 53 -d "$resolver" -j ACCEPT
done <<< "$DNS_SERVERS"

ipset create allowed-domains hash:net

while read -r cidr; do
  [[ "$cidr" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || { echo "ERROR: Invalid GitHub CIDR: $cidr"; exit 1; }
  ipset add -exist allowed-domains "$cidr"
done < <(echo "$gh_ranges" | jq -r '(.web + .api + .git)[]' | aggregate -q)

for domain in "${ALLOWED_DOMAINS[@]}"; do
  echo "Resolving $domain..."
  ips=$(dig +noall +answer A "$domain" | awk '$4 == "A" {print $5}')
  if [[ -z "$ips" ]]; then
    echo "WARN: Failed to resolve $domain; skipping"
    continue
  fi
  while read -r ip; do
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "ERROR: Invalid DNS IP for $domain: $ip"; exit 1; }
    ipset add -exist allowed-domains "$ip"
  done < <(echo "$ips")
done

HOST_IP=$(ip route | awk '/default/ {print $3; exit}')
[[ -n "$HOST_IP" ]] || { echo "ERROR: Failed to detect host IP"; exit 1; }
iptables -A OUTPUT -d "$HOST_IP" -j ACCEPT

iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

if curl --connect-timeout 5 https://example.com >/dev/null 2>&1; then
  echo "ERROR: Firewall verification failed - reached https://example.com"
  exit 1
fi
curl --connect-timeout 5 https://api.github.com/zen >/dev/null 2>&1 || {
  echo "ERROR: Firewall verification failed - cannot reach api.github.com"
  exit 1
}

touch "$READY_FLAG"
echo "Firewall configuration complete"
