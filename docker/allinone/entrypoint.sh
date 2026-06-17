#!/usr/bin/env bash
# SmartLoad all-in-one entrypoint.
#
# 1. Alias every docker-compose service hostname to 127.0.0.1 in /etc/hosts so
#    the unmodified service configs/env (which point at `timescaledb`, `redis`,
#    `otel-collector`, `telemetry`, `prometheus`, ... ) resolve in-container.
# 2. First-run initialise PostgreSQL + TimescaleDB and load the schema.
# 3. Hand off to supervisord, which runs every process.
set -euo pipefail

PGDATA=/var/lib/postgresql/data
PGBIN=/usr/lib/postgresql/16/bin
PG_PASSWORD="${TIMESCALEDB_PASSWORD:-changeme}"

# ── 1. /etc/hosts aliases ────────────────────────────────────────────────────
# Docker rewrites /etc/hosts at container start, so we (re)append at runtime.
HOSTS_ALIASES="timescaledb redis redis-exporter otel-collector prometheus \
grafana telemetry anomaly-detector forecasting rl-engine autoscaler \
policy-manager lb-sidecar load-balancer operator-ui demo-ui traffic-simulator \
smartload-load-balancer-1"
if ! grep -q "smartload all-in-one aliases" /etc/hosts 2>/dev/null; then
  {
    echo ""
    echo "# smartload all-in-one aliases"
    echo "127.0.0.1 ${HOSTS_ALIASES}"
  } >> /etc/hosts
fi

# ── 2. PostgreSQL + TimescaleDB ──────────────────────────────────────────────
mkdir -p "$PGDATA" /nginx-logs /var/lib/prometheus/data /var/lib/grafana/data \
         /var/log/supervisor
chown -R postgres:postgres "$PGDATA" /var/lib/postgresql
chmod 700 "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[entrypoint] initialising PostgreSQL cluster in $PGDATA"
  su postgres -c "$PGBIN/initdb -D '$PGDATA' --auth-local=trust --auth-host=md5 --encoding=UTF8"

  cat >> "$PGDATA/postgresql.conf" <<'EOF'

# ── SmartLoad all-in-one tuning ──────────────────────────────────────────────
listen_addresses = 'localhost'
shared_preload_libraries = 'timescaledb'
timescaledb.telemetry_level = 'off'
max_worker_processes = 16
timescaledb.max_background_workers = 8
EOF

  echo "[entrypoint] starting temporary postgres for bootstrap"
  su postgres -c "$PGBIN/pg_ctl -D '$PGDATA' -w -t 60 start"

  echo "[entrypoint] creating role password, database, schema"
  su postgres -c "psql -v ON_ERROR_STOP=1 --username postgres <<SQL
ALTER USER postgres WITH PASSWORD '${PG_PASSWORD}';
SELECT 'CREATE DATABASE smartloaddb' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'smartloaddb')\\gexec
SQL"
  su postgres -c "psql -v ON_ERROR_STOP=1 --username postgres --dbname smartloaddb -f /docker/init.sql"

  su postgres -c "$PGBIN/pg_ctl -D '$PGDATA' -m fast -w stop"
  echo "[entrypoint] PostgreSQL bootstrap complete"
else
  echo "[entrypoint] existing PostgreSQL cluster found — skipping init"
fi

# ── 3. supervisord ───────────────────────────────────────────────────────────
echo "[entrypoint] launching supervisord"
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
