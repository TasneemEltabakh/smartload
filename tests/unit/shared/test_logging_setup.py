"""
tests/unit/shared/test_logging_setup.py
──────────────────────────────────────────
Unit tests for services/shared/logging_setup.py — correlation id contextvar,
inbound-id extraction, the structured logger, and (where Flask is available)
the request middleware.
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest

_SERVICES = Path(__file__).resolve().parents[3] / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from shared.logging_setup import (  # noqa: E402
    CORRELATION_HEADER,
    extract_correlation_id,
    get_correlation_id,
    get_logger,
    install_correlation_middleware,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)


# ── correlation id ──────────────────────────────────────────────────────────────

def test_new_correlation_id_is_distinct_hex():
    a, b = new_correlation_id(), new_correlation_id()
    assert a != b
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)


def test_set_get_reset_correlation_id():
    assert get_correlation_id() is None
    token = set_correlation_id("abc123")
    try:
        assert get_correlation_id() == "abc123"
    finally:
        reset_correlation_id(token)
    assert get_correlation_id() is None


# ── extract_correlation_id ──────────────────────────────────────────────────────

def test_extract_prefers_explicit_header():
    headers = {CORRELATION_HEADER: "cid-1", "traceparent": "00-" + "a" * 32 + "-bbbbbbbbbbbbbbbb-01"}
    assert extract_correlation_id(headers) == "cid-1"


def test_extract_falls_back_to_traceparent():
    trace = "f" * 32
    headers = {"traceparent": f"00-{trace}-bbbbbbbbbbbbbbbb-01"}
    assert extract_correlation_id(headers) == trace


def test_extract_none_when_absent():
    assert extract_correlation_id({}) is None
    assert extract_correlation_id(None) is None


def test_extract_ignores_malformed_traceparent():
    assert extract_correlation_id({"traceparent": "garbage"}) is None
    assert extract_correlation_id({"traceparent": "00-tooshort-x-01"}) is None


# ── structured logger ────────────────────────────────────────────────────────────

def test_logger_emits_structured_line_with_cid():
    stream = io.StringIO()
    logger = get_logger("t_log_cid", stream=stream)
    token = set_correlation_id("trace-xyz")
    try:
        logger.info("hello world")
    finally:
        reset_correlation_id(token)
    out = stream.getvalue()
    assert "service=t_log_cid" in out
    assert "cid=trace-xyz" in out
    assert "msg=hello world" in out


def test_logger_shows_dash_when_no_cid():
    stream = io.StringIO()
    logger = get_logger("t_log_nocid", stream=stream)
    logger.info("no correlation set")
    assert "cid=-" in stream.getvalue()


def test_logger_is_idempotent_no_duplicate_handlers():
    stream = io.StringIO()
    get_logger("t_log_idem", stream=stream)
    logger = get_logger("t_log_idem", stream=stream)  # second call must not stack
    logger.info("once")
    # Exactly one line → one handler, not two.
    assert stream.getvalue().count("msg=once") == 1


# ── Flask middleware (only where Flask is installed) ────────────────────────────

def test_middleware_mints_and_echoes_correlation_id():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    install_correlation_middleware(app)

    @app.route("/probe")
    def _probe():
        return get_correlation_id() or "none"

    client = app.test_client()

    # No inbound header → a fresh id is minted, returned in body + echoed header.
    r = client.get("/probe")
    body = r.get_data(as_text=True)
    assert body != "none" and len(body) == 32
    assert r.headers.get(CORRELATION_HEADER) == body

    # Inbound header → propagated.
    r = client.get("/probe", headers={CORRELATION_HEADER: "inbound-cid"})
    assert r.get_data(as_text=True) == "inbound-cid"
    assert r.headers.get(CORRELATION_HEADER) == "inbound-cid"


def test_middleware_resets_cid_between_requests():
    pytest.importorskip("flask")
    import flask

    app = flask.Flask(__name__)
    install_correlation_middleware(app)

    @app.route("/probe")
    def _probe():
        return get_correlation_id() or "none"

    client = app.test_client()
    client.get("/probe", headers={CORRELATION_HEADER: "first"})
    # After the request completes, the contextvar is reset — no leak into the
    # next request's pre-handler state.
    assert get_correlation_id() is None
