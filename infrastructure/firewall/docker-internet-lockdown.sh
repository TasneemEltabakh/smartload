#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Block direct internet -> Docker container traffic.
#
# WHY: Docker publishes container ports by inserting its own iptables DNAT rules
# that BYPASS ufw's INPUT chain. So `ufw deny` does NOT stop the world from
# reaching any 0.0.0.0-published container port. The only supported hook to
# filter that traffic is the DOCKER-USER chain (consulted before Docker's own
# accept rules).
#
# WHAT: For packets arriving on the WAN interface and being forwarded to a
# container, allow only replies to connections the container itself opened;
# DROP everything else. This is INTERFACE-scoped (not port-scoped), so it can't
# be bypassed by a container whose internal port happens to be 80/443.
#
# Host services (SSH 22, Caddy 80/443) are NOT affected — they terminate in the
# host INPUT chain, which DOCKER-USER never sees. Caddy -> 127.0.0.1:<app> and
# inter-container traffic are NOT on the WAN interface, so they pass untouched.
#
# Net effect, combined with ufw (22/80/443): the ONLY public surface is Caddy.
# Idempotent — safe to re-run. Invoked at boot by docker-internet-lockdown.service.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Auto-detect the WAN interface (the one with the default route).
WAN_IF="${WAN_IF:-$(ip route get 8.8.8.8 2>/dev/null | grep -oE 'dev [^ ]+' | awk '{print $2}' | head -1)}"
WAN_IF="${WAN_IF:-eth0}"
echo "[lockdown] WAN interface = ${WAN_IF}"

apply() {
  local ipt="$1"
  # Wait for Docker to have created the DOCKER-USER chain.
  local i
  for i in $(seq 1 30); do
    "$ipt" -L DOCKER-USER -n >/dev/null 2>&1 && break
    sleep 1
  done
  "$ipt" -L DOCKER-USER -n >/dev/null 2>&1 || { echo "[lockdown] ${ipt}: no DOCKER-USER chain, skipping"; return 0; }

  # Remove any prior copies of our two rules (idempotency).
  while "$ipt" -C DOCKER-USER -i "$WAN_IF" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null; do
    "$ipt" -D DOCKER-USER -i "$WAN_IF" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  done
  while "$ipt" -C DOCKER-USER -i "$WAN_IF" -j DROP 2>/dev/null; do
    "$ipt" -D DOCKER-USER -i "$WAN_IF" -j DROP
  done

  # Insert at the TOP, DROP first then RETURN above it (final order: RETURN, DROP).
  "$ipt" -I DOCKER-USER 1 -i "$WAN_IF" -j DROP
  "$ipt" -I DOCKER-USER 1 -i "$WAN_IF" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  echo "[lockdown] ${ipt}: rules applied"
}

apply iptables
# IPv6: only if Docker maintains a v6 DOCKER-USER chain.
if ip6tables -L DOCKER-USER -n >/dev/null 2>&1; then
  apply ip6tables
fi

echo "[lockdown] done. Current IPv4 DOCKER-USER:"
iptables -L DOCKER-USER -n --line-numbers
