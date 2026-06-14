"""
tests/unit/operator-ui/test_docs_pages.py
───────────────────────────────────────────
Unit tests for services/operator-ui/bff/docs_pages.py — the pure HTML builder
for the AsyncAPI viewer page served at /api/asyncapi-docs.

Pure-Python: no Flask / httpx / redis, so it runs in the bare unit-tests CI
job (same constraint as test_engines.py — app.py imports httpx, which isn't
installed there, so we never import the full app here).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add services/operator-ui/bff/ to sys.path so we can import docs_pages
# directly (same pattern as test_engines.py).
_BFF = Path(__file__).resolve().parents[2].parent / "services" / "operator-ui" / "bff"
if str(_BFF) not in sys.path:
    sys.path.insert(0, str(_BFF))

from docs_pages import asyncapi_docs_html, _escape  # noqa: E402


def test_returns_complete_html_document():
    html = asyncapi_docs_html()
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html.rstrip()[-10:]


def test_loads_the_asyncapi_react_component_from_cdn():
    html = asyncapi_docs_html()
    # both the JS bundle and the stylesheet, pinned to the v2 major
    assert "@asyncapi/react-component@2/browser/standalone/index.js" in html
    assert "@asyncapi/react-component@2/styles/default.min.css" in html
    assert "AsyncApiStandalone.render(" in html


def test_points_the_viewer_at_the_served_spec_url():
    html = asyncapi_docs_html()
    # default spec URL appears in the fallback link (HTML) and as a JS string
    assert "/api/asyncapi.yaml" in html
    assert json.dumps("/api/asyncapi.yaml") in html  # the fetch {url: "..."} arg


def test_custom_spec_url_is_used():
    html = asyncapi_docs_html("/custom/spec.yaml", title="My Events")
    assert json.dumps("/custom/spec.yaml") in html
    assert "/api/asyncapi.yaml" not in html
    assert "<title>My Events</title>" in html


def test_no_unsubstituted_placeholders_remain():
    html = asyncapi_docs_html()
    for token in ("__TITLE__", "__CDN_BASE__", "__SPEC_URL__", "__SPEC_URL_JS__"):
        assert token not in html, f"placeholder {token} was not substituted"


def test_has_offline_fallback_and_noscript():
    html = asyncapi_docs_html()
    assert "<noscript>" in html
    assert 'id="fallback"' in html
    assert 'id="asyncapi"' in html  # the mount point


def test_escape_neutralises_html_metacharacters():
    assert _escape('<a href="x">&') == "&lt;a href=&quot;x&quot;&gt;&amp;"


def test_title_is_escaped_into_the_document():
    html = asyncapi_docs_html(title="A & B <x>")
    assert "<title>A &amp; B &lt;x&gt;</title>" in html
