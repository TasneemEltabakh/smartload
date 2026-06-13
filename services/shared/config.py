"""
services/shared/config.py
─────────────────────────
Typed environment-variable helpers + canonical service URLs.

Every control-plane service hand-rolls the same env parsing: `int(os.environ
.get("PORT", ...))`, the `== "true"` bool idiom, and a hardcoded
`postgresql://postgres:changeme@...` default DSN repeated verbatim across seven
files. This centralises it so:
  - bool / int / float parsing is consistent (and forgiving of "1"/"yes"/"on");
  - a *required* var that's missing fails fast with a clear message instead of
    silently falling back to a dev default (the `changeme` DSN footgun);
  - the Redis / TimescaleDB defaults live in one place.

Stdlib-only and side-effect-free, so it imports anywhere and unit-tests without
redis / psycopg2 / flask. Every reader takes an optional ``env`` mapping so
tests can inject a dict instead of mutating ``os.environ``.

This module is intentionally standalone — adopting it in the services is a
separate, CI-watched step so the primitive can land and be tested first.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}

# Canonical compose defaults — single source of truth for the dev stack.
DEFAULT_REDIS_URL = "redis://redis:6379"
DEFAULT_TIMESCALEDB_URL = "postgresql://postgres:changeme@timescaledb:5432/smartloaddb"


class ConfigError(RuntimeError):
    """A required env var is missing, or a value can't be parsed."""


def _lookup(name: str, env: Optional[Mapping[str, str]]) -> Optional[str]:
    source = os.environ if env is None else env
    val = source.get(name)
    # Treat an empty string the same as unset — a blank env var in compose
    # (``FOO:`` / ``FOO=``) should fall back to the default, not "".
    if val is None or val == "":
        return None
    return val


def env_str(name, default=None, *, required=False, env=None):
    """Return the env var as a string, the default, or raise if required+missing."""
    val = _lookup(name, env)
    if val is None:
        if required:
            raise ConfigError(f"required env var {name!r} is not set")
        return default
    return val


def env_int(name, default=None, *, required=False, env=None):
    raw = env_str(name, None, required=required, env=env)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"env var {name!r}={raw!r} is not an int") from exc


def env_float(name, default=None, *, required=False, env=None):
    raw = env_str(name, None, required=required, env=env)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"env var {name!r}={raw!r} is not a float") from exc


def env_bool(name, default=False, *, env=None):
    """Parse a boolean env var. Accepts 1/true/yes/on (and 0/false/no/off).
    Raises ConfigError on an unrecognised value rather than silently coercing."""
    raw = _lookup(name, env)
    if raw is None:
        return default
    low = raw.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ConfigError(f"env var {name!r}={raw!r} is not a boolean")


def redis_url(*, required=False, env=None):
    """REDIS_URL or the canonical default."""
    return env_str("REDIS_URL", DEFAULT_REDIS_URL, required=required, env=env)


def timescaledb_url(*, required=False, env=None):
    """TIMESCALEDB_URL or the dev default.

    Pass ``required=True`` in a production bootstrap to fail fast instead of
    silently connecting with the ``changeme`` dev credentials.
    """
    return env_str("TIMESCALEDB_URL", DEFAULT_TIMESCALEDB_URL, required=required, env=env)
