"""
services/shared/logging_setup.py
────────────────────────────────
Structured logging + correlation IDs (#143).

Three services use the stdlib ``logging`` module, three use bare
``print(..., flush=True)`` — no common format and no per-request trace. This
provides:
  - ``get_logger(service)``: a logger emitting ``key=value`` structured lines
    that always carry the current correlation id (``cid=-`` when unset);
  - a contextvar-based correlation id (get / set / reset / new);
  - ``extract_correlation_id(headers)``: read an inbound ``X-Correlation-ID`` or
    a W3C ``traceparent`` trace-id;
  - ``install_correlation_middleware(app)``: a Flask hook that mints/propagates
    the id per request and echoes it on the response.

The pure pieces (id extraction, the contextvar, the formatter) unit-test
without Flask; the middleware install lazily imports Flask so the module loads
bare.

Module-first: not yet wired into the services — CI-watched follow-up.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Optional

CORRELATION_HEADER = "X-Correlation-ID"
_TRACEPARENT_HEADER = "traceparent"
_HEX = set("0123456789abcdef")

_correlation_id: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)


# ── correlation id ──────────────────────────────────────────────────────────────

def new_correlation_id() -> str:
    """A fresh correlation id (uuid4 hex)."""
    return uuid.uuid4().hex


def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()


def set_correlation_id(cid):
    """Set the current correlation id; returns the contextvar Token for reset."""
    return _correlation_id.set(cid)


def reset_correlation_id(token) -> None:
    _correlation_id.reset(token)


def _parse_traceparent(value):
    """Extract the 32-hex trace-id from a W3C traceparent
    (``version-traceid-spanid-flags``). None if it doesn't parse."""
    if not value:
        return None
    parts = value.split("-")
    if len(parts) >= 3 and len(parts[1]) == 32 and set(parts[1].lower()) <= _HEX:
        return parts[1]
    return None


def extract_correlation_id(headers, *, header=CORRELATION_HEADER):
    """Return the inbound correlation id from a headers mapping: the explicit
    correlation header if present, else the trace-id from ``traceparent``, else
    None. ``headers`` is any ``.get``-able mapping (Flask ``request.headers`` or
    a plain dict in tests)."""
    if headers is None:
        return None
    cid = headers.get(header)
    if cid:
        return cid
    return _parse_traceparent(headers.get(_TRACEPARENT_HEADER))


# ── structured logging ──────────────────────────────────────────────────────────

class _CorrelationFilter(logging.Filter):
    """Inject the current correlation id onto every record as ``correlation_id``."""

    def filter(self, record):
        record.correlation_id = get_correlation_id() or "-"
        return True


def get_logger(service_name, *, level=logging.INFO, stream=None):
    """Return a logger for ``service_name`` emitting structured lines:

        ts=... level=... service=<name> cid=<correlation> msg=...

    Idempotent — repeated calls don't stack handlers (a marker attribute guards
    against double-add)."""
    logger = logging.getLogger(service_name)
    logger.setLevel(level)
    logger.propagate = False
    if not any(getattr(h, "_smartload", False) for h in logger.handlers):
        handler = logging.StreamHandler(stream or sys.stdout)
        handler._smartload = True  # marker so repeated get_logger() calls don't stack
        handler.addFilter(_CorrelationFilter())
        handler.setFormatter(logging.Formatter(
            "ts=%(asctime)s level=%(levelname)s "
            f"service={service_name} "
            "cid=%(correlation_id)s msg=%(message)s"
        ))
        logger.addHandler(handler)
    return logger


# ── Flask middleware ─────────────────────────────────────────────────────────────

def install_correlation_middleware(app, *, header=CORRELATION_HEADER):
    """Wire a Flask app so each request carries a correlation id (inbound header
    / traceparent, or a fresh one) for its lifetime via ``get_correlation_id()``,
    echoed back on the response header."""
    from flask import g, request  # lazy — only needed when wiring Flask

    @app.before_request
    def _set_cid():
        cid = extract_correlation_id(request.headers, header=header) or new_correlation_id()
        g._correlation_token = set_correlation_id(cid)
        g._correlation_id = cid

    @app.after_request
    def _echo_cid(response):
        cid = getattr(g, "_correlation_id", None)
        if cid:
            response.headers[header] = cid
        token = getattr(g, "_correlation_token", None)
        if token is not None:
            try:
                reset_correlation_id(token)
            except (LookupError, ValueError):
                pass
        return response

    return app
