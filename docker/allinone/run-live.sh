#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SmartLoad all-in-one — LIVE / public deployment launcher.
#
# Runs the single-container SmartLoad stack bound to 127.0.0.1 ONLY, so the
# host-level Caddy reverse proxy is the *only* thing that can reach the
# dashboards. Nothing here is published on 0.0.0.0 — the public surface is
# exclusively ports 80/443 served by Caddy (see infrastructure/reverse-proxy/).
#
# Public mapping (set up in /etc/caddy/sites/smartload.caddy):
#   smartload-demo.cie21grad.systems        -> 127.0.0.1:8091  (demo UI,        open)
#   smartload-operator.cie21grad.systems    -> 127.0.0.1:8090  (operator UI,    basic auth)
#   smartload-grafana.cie21grad.systems     -> 127.0.0.1:3000  (Grafana)
#   smartload-prometheus.cie21grad.systems  -> 127.0.0.1:9090  (Prometheus)
#   smartload-locust.cie21grad.systems      -> 127.0.0.1:8089  (Locust)
#   smartload-lb.cie21grad.systems          -> 127.0.0.1:8080  (NGINX LB :80)
#
# Secrets (Grafana admin password, TimescaleDB password) come from
# docker/allinone/.live.env (gitignored). Re-running this script recreates the
# container; the all-in-one image is ephemeral by design (demo data repopulates).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${HERE}/.live.env"
IMAGE="${SMARTLOAD_IMAGE:-smartload-allinone:latest}"
NAME="${SMARTLOAD_CONTAINER:-smartload-aio}"
BIND="127.0.0.1"   # never 0.0.0.0 — only Caddy should reach these

# Load secrets if present (else fall back to insecure defaults with a warning).
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
else
  echo "WARNING: ${ENV_FILE} not found — using insecure default passwords." >&2
fi
: "${GRAFANA_PASSWORD:=admin}"
: "${TIMESCALEDB_PASSWORD:=changeme}"

# Public hostname Grafana should advertise (fixes login redirects behind proxy).
GRAFANA_DOMAIN="${GRAFANA_DOMAIN:-smartload-grafana.cie21grad.systems}"

echo "[run-live] (re)creating container '${NAME}' from '${IMAGE}', bound to ${BIND}"
docker rm -f "${NAME}" >/dev/null 2>&1 || true

docker run -d \
  --name "${NAME}" \
  --restart unless-stopped \
  -e GRAFANA_PASSWORD="${GRAFANA_PASSWORD}" \
  -e TIMESCALEDB_PASSWORD="${TIMESCALEDB_PASSWORD}" \
  -e GF_SERVER_ROOT_URL="https://${GRAFANA_DOMAIN}/" \
  -e GF_SERVER_DOMAIN="${GRAFANA_DOMAIN}" \
  -p ${BIND}:8090:8090 \
  -p ${BIND}:8091:8091 \
  -p ${BIND}:3000:3000 \
  -p ${BIND}:9090:9090 \
  -p ${BIND}:8089:8089 \
  -p ${BIND}:8080:80 \
  "${IMAGE}"

# ── Live asset patches (deployment-specific; avoids a full image rebuild) ─────
# 1. Demo "Dashboards" page embeds Grafana from results.json's grafana.baseUrl,
#    which ships as http://localhost:3000 (unreachable + mixed-content over HTTPS).
#    Repoint it at the public Grafana subdomain.
# 2. Defensive: scrub the legacy "S. Rahman" sample identity from the operator
#    bundle if the image predates the source fix (no-op on rebuilt images).
patch_live_assets() {
  local demo_results="/opt/smartload/tools/demo-ui/web/dist/results/results.json"
  local op_assets="/opt/smartload/services/operator-ui/web/dist/assets"
  docker exec "${NAME}" sh -c "
    sed -i 's#http://localhost:3000#https://${GRAFANA_DOMAIN}#g' '${demo_results}' 2>/dev/null || true
    for f in ${op_assets}/*.js; do
      [ -f \"\$f\" ] || continue
      sed -i 's#initials:\"SR\",name:\"S. Rahman\",role:\"Reliability operator\"#initials:\"SL\",name:\"SmartLoad\",role:\"Operator console\"#g' \"\$f\"
      sed -i 's#S\. Rahman#Operator#g' \"\$f\"
    done
  " && echo "[run-live] live asset patches applied (grafana baseUrl + operator identity)"
}

# 3. Sync the fixed Grafana dashboards from the repo (the image may carry older
#    copies with the `window` reserved-keyword bug / stale forecast filters), and
#    seed the real captured scale events so the Scaling/Forecast panels populate.
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
patch_dashboards_and_seed() {
  local dash="${REPO_ROOT}/infrastructure/grafana/dashboards"
  local seed="${HERE}/seed-scaling-events.sql"
  if [[ -d "$dash" ]]; then
    docker cp "$dash/." "${NAME}:/var/lib/grafana/dashboards/" 2>/dev/null \
      && echo "[run-live] synced Grafana dashboards from repo"
  fi
  if [[ -f "$seed" ]]; then
    docker exec -i "${NAME}" psql -U postgres -d smartloaddb < "$seed" >/dev/null 2>&1 \
      && echo "[run-live] seeded real scale events into scaling_events"
  fi
}

echo "[run-live] waiting for health, then patching live assets…"
for i in $(seq 1 24); do
  st="$(docker inspect "${NAME}" --format '{{.State.Health.Status}}' 2>/dev/null || echo '?')"
  [ "$st" = "healthy" ] && break
  sleep 5
done
patch_live_assets
patch_dashboards_and_seed

echo "[run-live] started. Reachability:"
echo "  curl -sI http://127.0.0.1:8091/   # demo"
echo "  curl -sI http://127.0.0.1:8090/   # operator"
echo "  docker logs -f ${NAME}"
