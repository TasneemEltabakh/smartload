#!/bin/bash
set -e

echo "[INFO] Creating smartload database..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<-EOSQL
    SELECT 'CREATE DATABASE smartload'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'smartload')\gexec
EOSQL

echo "[INFO] Converting schema line endings and loading..."
tr -d '\r' < /docker-entrypoint-initdb.d/01-init-schema.sql > /tmp/init-schema-unix.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "smartload" -f /tmp/init-schema-unix.sql

echo "[INFO] Schema initialized successfully"