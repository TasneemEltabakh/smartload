# Schema migrations

`infrastructure/timescaledb/init.sql` is the **full current schema**, applied
automatically by Postgres on a *fresh* `timescaledb-data` volume (the
`/docker-entrypoint-initdb.d/` contract). It does **not** run again once the
volume exists — so it cannot deliver an additive schema change (a new column,
index, hypertable) to an already-running deployment.

This directory holds the **incremental deltas** for those existing
deployments. Each file is a numbered, idempotent SQL migration:

```
NNNN_description.sql      # 0001_scaling_events_mechanism.sql, 0002_…
```

`scripts/migrate.py` applies them in order, exactly once each, tracking
applied versions in a `schema_migrations(version, applied_at)` table. It is
safe to run repeatedly — applied migrations are skipped.

## Running

Fresh deployments need nothing — `init.sql` already contains the latest schema.

Existing deployments, after pulling a release that adds a migration:

```bash
# From the host (needs psycopg2-binary), pointing at the running DB:
TIMESCALEDB_URL=postgresql://postgres:$TIMESCALEDB_PASSWORD@localhost:5432/smartloaddb \
    python scripts/migrate.py

# Preview without applying:
python scripts/migrate.py --dry-run
```

`--migrations-dir DIR` overrides the default (`infrastructure/migrations/`);
`--dsn` overrides `TIMESCALEDB_URL`.

## Authoring a migration

1. Add the change to **both** `init.sql` (so fresh deployments get it) **and**
   a new `NNNN_*.sql` here (so existing deployments get it). Keep them in sync.
2. Make it idempotent where practical (`ADD COLUMN IF NOT EXISTS`,
   `CREATE INDEX IF NOT EXISTS`, `if_not_exists => TRUE`) so a
   partially-applied environment re-converges cleanly.
3. One logical change per file; each runs in its own transaction.

## Follow-up

Wiring a one-shot `migrate` compose service (profile-gated so it doesn't run on
the default `docker compose up`) would let `docker compose run --rm migrate`
apply pending migrations against the stack's DB without a host Python env —
tracked as an ops nicety, not yet shipped.
