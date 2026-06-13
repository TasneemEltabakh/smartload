#!/usr/bin/env python3
"""
scripts/migrate.py
──────────────────
Lightweight schema migration runner for SmartLoad's TimescaleDB.

`infrastructure/timescaledb/init.sql` only runs on a *fresh* data volume
(Postgres' /docker-entrypoint-initdb.d/ contract), so it cannot apply an
additive schema change to a deployment whose `timescaledb-data` volume
already exists. This runner closes that gap: it applies the numbered
`*.sql` files under infrastructure/migrations/ in order, exactly once
each, tracking applied versions in a `schema_migrations` table. It is
safe to run repeatedly — already-applied migrations are skipped.

The design deliberately avoids Alembic/SQLAlchemy: SmartLoad's persistence
is raw SQL (init.sql + the constants in services/shared/queries.py), so a
numbered-SQL runner is a closer fit and adds no heavy dependency (only
psycopg2, already used by every service).

Usage:
    TIMESCALEDB_URL=postgresql://postgres:...@host:5432/smartloaddb \
        python scripts/migrate.py [--migrations-dir DIR] [--dry-run]

Migration files are named `NNNN_description.sql` (zero-padded ordinal +
snake_case description). Each runs in its own transaction; a failure rolls
that migration back and aborts the run (later migrations are not applied).
Make every migration idempotent where practical (e.g.
`ADD COLUMN IF NOT EXISTS`) so a partially-applied environment re-converges
cleanly.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MIGRATIONS_DIR = _REPO_ROOT / "infrastructure" / "migrations"
# Matches the per-service default so a misconfigured host doesn't silently
# point at a different DB; override with TIMESCALEDB_URL / --dsn.
DEFAULT_DSN = "postgresql://postgres:changeme@timescaledb:5432/smartloaddb"

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT        PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# ── pure helpers (unit-tested without a DB) ───────────────────────────────────

def discover_migrations(migrations_dir):
    """Return [(version, path), …] for every `*.sql` under migrations_dir,
    ordered by filename. Version is the filename stem, e.g.
    '0001_scaling_events_mechanism'."""
    d = Path(migrations_dir)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.sql"), key=lambda p: p.name)
    return [(p.stem, p) for p in files]


def pending_migrations(all_migrations, applied):
    """Filter `all_migrations` [(version, path), …] to those whose version is
    not in `applied`, preserving order."""
    applied = set(applied)
    return [(v, p) for (v, p) in all_migrations if v not in applied]


# ── DB side ───────────────────────────────────────────────────────────────────

def _connect(dsn):
    import psycopg2  # lazy: keeps the pure helpers importable without psycopg2
    return psycopg2.connect(dsn)


def applied_versions(conn):
    """Ensure the tracking table exists and return the set of applied versions."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_MIGRATIONS_DDL)
        conn.commit()
        cur.execute("SELECT version FROM schema_migrations;")
        return {row[0] for row in cur.fetchall()}


def apply_migration(conn, version, sql_path):
    """Apply one migration in a transaction and record its version."""
    sql = Path(sql_path).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        try:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s);", (version,)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def run(dsn, migrations_dir, dry_run=False):
    """Apply all pending migrations. Returns the list of versions applied."""
    all_migrations = discover_migrations(migrations_dir)
    conn = _connect(dsn)
    try:
        applied = applied_versions(conn)
        pending = pending_migrations(all_migrations, applied)
        if not pending:
            print(f"[migrate] up to date — {len(applied)} applied, 0 pending")
            return []
        done = []
        for version, path in pending:
            if dry_run:
                print(f"[migrate] (dry-run) would apply {version}")
                continue
            print(f"[migrate] applying {version} …")
            apply_migration(conn, version, path)
            done.append(version)
        print(f"[migrate] done — applied {len(done)} migration(s)")
        return done
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply SmartLoad TimescaleDB migrations.")
    parser.add_argument("--migrations-dir", default=str(DEFAULT_MIGRATIONS_DIR))
    parser.add_argument("--dsn", default=os.environ.get("TIMESCALEDB_URL", DEFAULT_DSN))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        run(args.dsn, args.migrations_dir, dry_run=args.dry_run)
    except Exception as exc:  # surface a clear non-zero exit for CI / ops
        print(f"[migrate] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
