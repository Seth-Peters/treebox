"""Execute the bundled scripts with local paths and substitute network commands."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from treebox import assets


@pytest.fixture
def firewall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    template = assets.template_dir("default")
    ready = tmp_path / "ready"
    allowlist = tmp_path / "allowed-domains.sh"
    resolvers = tmp_path / "resolv.conf"
    resolvers.write_text("nameserver 192.0.2.53\n")
    shutil.copyfile(template / "allowed-domains.sh", allowlist)
    for script in ("init-firewall.sh", "post-create.sh"):
        source = (template / script).read_text()
        source = source.replace("/run/treebox-firewall-ready", shlex.quote(str(ready)))
        source = source.replace("/usr/local/share/allowed-domains.sh", shlex.quote(str(allowlist)))
        source = source.replace("/etc/resolv.conf", shlex.quote(str(resolvers)))
        (tmp_path / script).write_text(source)
    commands = tmp_path / "commands.sh"
    commands.write_text(
        r"""
tables() {
  local family=$1
  local IFS=' '
  shift
  case "$1" in
    -P) printf '%s\n' "$3" > "$TEST_FIREWALL/$family-$2" ;;
    -F) : > "$TEST_FIREWALL/$family-rules" ;;
    -A) printf '%s\n' "$*" >> "$TEST_FIREWALL/$family-rules" ;;
    -S) printf -- '-P OUTPUT %s\n' "$(cat "$TEST_FIREWALL/$family-OUTPUT")" ;;
  esac
}
iptables() { tables ipv4 "$@"; }
ip6tables() { tables ipv6 "$@"; }
iptables-save() { :; }
ipset() { [[ "${FAIL_AT:-}" != ipset ]]; }
curl() {
  case "${!#}" in
    https://api.github.com/meta)
      [[ "${FAIL_AT:-}" != fetch ]] || return 22
      printf '%s\n' '{"web":["140.82.112.0/20"],"api":[],"git":[]}' ;;
    https://example.com) [[ "${FAIL_AT:-}" == blocked-probe ]] ;;
    https://api.github.com/zen) [[ "${FAIL_AT:-}" != allowed-probe ]] ;;
    *) return 99 ;;
  esac
}
jq() {
  cat >/dev/null
  if [[ "$1" == -r ]]; then printf '%s\n' '140.82.112.0/20'; fi
}
aggregate() { cat; }
dig() { printf '%s\n' 'api.github.com. 60 IN A 140.82.112.3'; }
ip() { printf '%s\n' 'default via 172.18.0.1 dev eth0'; }
uv() { printf '%s\n' "$*" >> "$TEST_FIREWALL/workspace-setup"; }
playwright-cli() { :; }
"""
    )
    monkeypatch.setenv("BASH_ENV", str(commands))
    monkeypatch.setenv("TEST_FIREWALL", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FAIL_AT", raising=False)
    for family in ("ipv4", "ipv6"):
        for chain in ("INPUT", "FORWARD", "OUTPUT"):
            (tmp_path / f"{family}-{chain}").write_text("ACCEPT\n")
        (tmp_path / f"{family}-rules").write_text("")
    return tmp_path


def _run(firewall: Path, script: str = "init-firewall.sh") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(firewall / script)], cwd=firewall, capture_output=True, text=True
    )


def _rules(firewall: Path) -> list[list[str]]:
    return [shlex.split(line) for line in (firewall / "ipv4-rules").read_text().splitlines()]


def test_firewall_blocks_workspace_setup_until_ready(firewall: Path, monkeypatch):
    overlay = json.loads((assets.template_dir("default") / "firewall.json").read_text())
    for key, value in overlay["env"].items():
        monkeypatch.setenv(key, value)
    (firewall / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")

    blocked = _run(firewall, "post-create.sh")
    assert blocked.returncode == 1
    assert "refusing to run workspace setup" in blocked.stderr
    assert not (firewall / "workspace-setup").exists()

    initialized = _run(firewall)
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    assert (firewall / "ready").is_file()
    allowed = _run(firewall, "post-create.sh")
    assert allowed.returncode == 0, allowed.stderr
    assert (firewall / "workspace-setup").read_text() == "sync\n"


@pytest.mark.parametrize("ports", [None, "", "5432 8080", "65535"])
def test_firewall_opens_only_operator_tcp_host_ports(firewall: Path, ports: str | None):
    if ports is not None:
        (firewall / "allowed-domains.sh").write_text(
            "ALLOWED_DOMAINS=(api.github.com)\n"
            + (f"ALLOWED_HOST_PORTS=({ports})\n" if ports else "")
        )

    result = _run(firewall)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (firewall / "ipv4-OUTPUT").read_text() == "DROP\n"
    assert (firewall / "ready").is_file()
    rules = _rules(firewall)
    host_rules = [rule for rule in rules if "172.18.0.1" in rule]
    assert host_rules == [
        ["-A", "OUTPUT", "-d", "172.18.0.1", "-p", "tcp", "--dport", port, "-j", "ACCEPT"]
        for port in (ports or "").split()
    ]
    assert rules[-2:] == [
        ["-A", "OUTPUT", "-m", "set", "--match-set", "allowed-domains", "dst", "-j", "ACCEPT"],
        ["-A", "OUTPUT", "-j", "REJECT", "--reject-with", "icmp-admin-prohibited"],
    ]


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("policy", ["ACCEPT", "DROP"])
def test_firewall_skip_requires_ready_flag_and_live_drop_policy(
    firewall: Path, ready: bool, policy: str
):
    if ready:
        (firewall / "ready").touch()
    (firewall / "ipv4-OUTPUT").write_text(policy + "\n")
    previous_rules = "-A OUTPUT -d 198.51.100.42 -j ACCEPT\n"
    (firewall / "ipv4-rules").write_text(previous_rules)

    result = _run(firewall)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (firewall / "ready").is_file()
    assert (firewall / "ipv4-OUTPUT").read_text() == "DROP\n"
    if ready and policy == "DROP":
        assert (firewall / "ipv4-rules").read_text() == previous_rules
        assert "skipping reconfiguration" in result.stdout
    else:
        assert ["-A", "OUTPUT", "-d", "198.51.100.42", "-j", "ACCEPT"] not in _rules(firewall)
        assert _rules(firewall)[-1] == [
            "-A",
            "OUTPUT",
            "-j",
            "REJECT",
            "--reject-with",
            "icmp-admin-prohibited",
        ]


@pytest.mark.parametrize("failure", ["fetch", "ipset", "blocked-probe", "allowed-probe"])
def test_firewall_failure_drops_all_traffic(firewall: Path, monkeypatch, failure: str):
    monkeypatch.setenv("FAIL_AT", failure)
    _assert_failure_drops_all_traffic(firewall)


@pytest.mark.parametrize("port", ["0", "65536", "080", "-1", "http"])
def test_firewall_invalid_host_port_fails_closed(firewall: Path, port: str):
    (firewall / "allowed-domains.sh").write_text(
        f"ALLOWED_DOMAINS=(api.github.com)\nALLOWED_HOST_PORTS=({port})\n"
    )
    _assert_failure_drops_all_traffic(firewall)


def _assert_failure_drops_all_traffic(firewall: Path) -> None:
    (firewall / "ready").touch()
    (firewall / "ipv4-rules").write_text("-A OUTPUT -d 172.18.0.1 -j ACCEPT\n")

    result = _run(firewall)

    assert result.returncode != 0
    assert "all traffic dropped" in result.stderr
    assert not (firewall / "ready").exists()
    for family in ("ipv4", "ipv6"):
        for chain in ("INPUT", "FORWARD", "OUTPUT"):
            assert (firewall / f"{family}-{chain}").read_text() == "DROP\n"
        assert (firewall / f"{family}-rules").read_text() == ""
