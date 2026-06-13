"""
tests/unit/shared/test_config.py
──────────────────────────────────
Unit tests for the shared typed env helpers (services/shared/config.py).

Hermetic: every reader takes an injected ``env`` dict, so no os.environ
mutation and no ordering coupling between tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICES = Path(__file__).resolve().parents[3] / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from shared.config import (  # noqa: E402
    DEFAULT_REDIS_URL,
    DEFAULT_TIMESCALEDB_URL,
    ConfigError,
    env_bool,
    env_float,
    env_int,
    env_str,
    redis_url,
    timescaledb_url,
)


# ── env_str ───────────────────────────────────────────────────────────────────

def test_env_str_present():
    assert env_str("X", env={"X": "hello"}) == "hello"


def test_env_str_missing_returns_default():
    assert env_str("X", "fallback", env={}) == "fallback"


def test_env_str_empty_treated_as_missing():
    assert env_str("X", "fallback", env={"X": ""}) == "fallback"


def test_env_str_required_missing_raises():
    with pytest.raises(ConfigError):
        env_str("X", required=True, env={})


# ── env_int / env_float ─────────────────────────────────────────────────────────

def test_env_int_parses():
    assert env_int("N", env={"N": "42"}) == 42


def test_env_int_default():
    assert env_int("N", 7, env={}) == 7


def test_env_int_invalid_raises():
    with pytest.raises(ConfigError):
        env_int("N", env={"N": "notanint"})


def test_env_float_parses():
    assert env_float("F", env={"F": "1.5"}) == 1.5


def test_env_float_invalid_raises():
    with pytest.raises(ConfigError):
        env_float("F", env={"F": "x"})


# ── env_bool ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "y", "  True  "])
def test_env_bool_truthy(raw):
    assert env_bool("B", env={"B": raw}) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "n"])
def test_env_bool_falsy(raw):
    assert env_bool("B", env={"B": raw}) is False


def test_env_bool_default_when_missing():
    assert env_bool("B", default=True, env={}) is True
    assert env_bool("B", env={}) is False


def test_env_bool_invalid_raises():
    with pytest.raises(ConfigError):
        env_bool("B", env={"B": "maybe"})


# ── url helpers ─────────────────────────────────────────────────────────────────

def test_redis_url_default_and_override():
    assert redis_url(env={}) == DEFAULT_REDIS_URL
    assert redis_url(env={"REDIS_URL": "redis://x:6379"}) == "redis://x:6379"


def test_timescaledb_url_default_and_override():
    assert timescaledb_url(env={}) == DEFAULT_TIMESCALEDB_URL
    assert timescaledb_url(env={"TIMESCALEDB_URL": "postgresql://h/db"}) == "postgresql://h/db"


def test_timescaledb_url_required_fails_fast():
    """The whole point: in a production bootstrap, a missing DSN should raise
    rather than silently connect with the `changeme` dev default."""
    with pytest.raises(ConfigError):
        timescaledb_url(required=True, env={})
