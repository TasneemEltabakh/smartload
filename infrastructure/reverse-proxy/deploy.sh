#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Deploy the shared Caddy reverse-proxy config from this repo to the host.
#
#   - Installs infrastructure/reverse-proxy/Caddyfile      -> /etc/caddy/Caddyfile
#   - Renders sites/smartload.caddy (substituting the operator basic-auth bcrypt
#     hash from docker/allinone/.live.env) -> /etc/caddy/sites/smartload.caddy
#   - Validates, then reloads the systemd "caddy" service (zero-downtime).
#
# Idempotent. Re-run after editing any config file. Requires sudo.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RP_DIR="${REPO_ROOT}/infrastructure/reverse-proxy"
SECRETS="${REPO_ROOT}/docker/allinone/.live.env"

[[ -f "$SECRETS" ]] || { echo "ERROR: missing $SECRETS (run-live secrets)"; exit 1; }
# shellcheck disable=SC1090
source "$SECRETS"
: "${OPERATOR_BASICAUTH_BCRYPT:?OPERATOR_BASICAUTH_BCRYPT not set in .live.env}"

echo "[deploy] backing up current /etc/caddy/Caddyfile (if any)"
sudo cp -n /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date -u +%Y%m%dT%H%M%SZ)" 2>/dev/null || true

echo "[deploy] installing main Caddyfile + sites/"
sudo install -d -m 0755 /etc/caddy/sites
sudo install -m 0644 "${RP_DIR}/Caddyfile" /etc/caddy/Caddyfile

echo "[deploy] rendering sites/smartload.caddy with operator basic-auth hash"
sed "s|__OPERATOR_BCRYPT__|${OPERATOR_BASICAUTH_BCRYPT}|" "${RP_DIR}/sites/smartload.caddy" \
  | sudo tee /etc/caddy/sites/smartload.caddy >/dev/null
sudo chmod 0644 /etc/caddy/sites/smartload.caddy

echo "[deploy] validating"
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

echo "[deploy] reloading caddy"
sudo systemctl reload caddy
echo "[deploy] done. Active sites:"
sudo ls -1 /etc/caddy/sites/
