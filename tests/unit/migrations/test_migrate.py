"""
tests/unit/migrations/test_migrate.py
───────────────────────────────────────
Unit tests for the pure (DB-free) logic of scripts/migrate.py: migration
file discovery + ordering + pending-filter. psycopg2 is imported lazily by
the runner, so these tests need no DB and no psycopg2.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2].parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from migrate import discover_migrations, pending_migrations  # noqa: E402


def _touch(d, name):
    (d / name).write_text("-- noop\n", encoding="utf-8")


def test_discover_orders_by_version(tmp_path):
    _touch(tmp_path, "0002_b.sql")
    _touch(tmp_path, "0001_a.sql")
    _touch(tmp_path, "0010_c.sql")
    assert [v for v, _ in discover_migrations(tmp_path)] == ["0001_a", "0002_b", "0010_c"]


def test_discover_ignores_non_sql(tmp_path):
    _touch(tmp_path, "0001_a.sql")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "0002_b.txt").write_text("x", encoding="utf-8")
    assert [v for v, _ in discover_migrations(tmp_path)] == ["0001_a"]


def test_discover_empty_or_missing_dir(tmp_path):
    assert discover_migrations(tmp_path) == []
    assert discover_migrations(tmp_path / "does-not-exist") == []


def test_pending_filters_applied(tmp_path):
    for n in ("0001_a.sql", "0002_b.sql", "0003_c.sql"):
        _touch(tmp_path, n)
    allm = discover_migrations(tmp_path)
    pending = pending_migrations(allm, {"0001_a", "0002_b"})
    assert [v for v, _ in pending] == ["0003_c"]


def test_pending_none_when_all_applied(tmp_path):
    _touch(tmp_path, "0001_a.sql")
    assert pending_migrations(discover_migrations(tmp_path), {"0001_a"}) == []


def test_pending_all_when_none_applied(tmp_path):
    _touch(tmp_path, "0001_a.sql")
    _touch(tmp_path, "0002_b.sql")
    assert [v for v, _ in pending_migrations(discover_migrations(tmp_path), set())] == [
        "0001_a",
        "0002_b",
    ]


def test_real_migrations_dir_discovers_0001():
    """Smoke: the committed migrations dir is discoverable and ordered, and
    the first real migration is present."""
    real = Path(__file__).resolve().parents[2].parent / "infrastructure" / "migrations"
    versions = [v for v, _ in discover_migrations(real)]
    assert "0001_scaling_events_mechanism" in versions
    assert versions == sorted(versions)
