"""
services/operator-ui/bff/docs_pages.py
───────────────────────────────────────
Pure HTML page builders for the operator-UI documentation surfaces.

The HTTP surface (OpenAPI) is rendered by flask-swagger-ui. The asynchronous
surface (AsyncAPI — the five Redis control-bus channels + the operator-UI SSE
stream) has no equivalent Flask package, so this module returns a small viewer
page that loads the official @asyncapi/react-component from a CDN and points it
at the spec the BFF already serves at /api/asyncapi.yaml.

Mirrors the Swagger-UI model: the viewer assets come from elsewhere, the spec
is fetched same-origin at runtime, so the canonical artifact stays the single
mounted YAML (docs/asyncapi/smartload-v1.yaml) — no regeneration on change.

Pure (no Flask / httpx / redis imports) so it unit-tests in the bare CI
unit-tests job, the same way engines.py does.
"""

from __future__ import annotations

import json

# Pinned to the v2 major of the standalone bundle (supports AsyncAPI 3.0).
_REACT_COMPONENT_VERSION = "2"
_CDN_BASE = f"https://unpkg.com/@asyncapi/react-component@{_REACT_COMPONENT_VERSION}"

_ASYNCAPI_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <link rel="stylesheet" href="__CDN_BASE__/styles/default.min.css">
  <style>
    html, body { margin: 0; background: #fff; }
    #asyncapi { min-height: 100vh; }
    #fallback {
      display: none;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      padding: 24px; color: #842029; background: #f8d7da; border-bottom: 1px solid #f1aeb5;
    }
    #fallback a { color: #842029; }
  </style>
</head>
<body>
  <div id="fallback">
    Could not load the AsyncAPI viewer (no network access to the CDN?).
    The raw spec is served at <a href="__SPEC_URL__">__SPEC_URL__</a>.
  </div>
  <noscript>
    This page renders the AsyncAPI document with JavaScript. The raw spec is at
    <a href="__SPEC_URL__">__SPEC_URL__</a>.
  </noscript>
  <div id="asyncapi"></div>
  <script src="__CDN_BASE__/browser/standalone/index.js"></script>
  <script>
    (function () {
      function fail() {
        var f = document.getElementById("fallback");
        if (f) { f.style.display = "block"; }
      }
      if (typeof AsyncApiStandalone === "undefined") { fail(); return; }
      try {
        AsyncApiStandalone.render({
          schema: { url: __SPEC_URL_JS__ },
          config: { show: { sidebar: true, info: true, servers: true, operations: true, messages: true, schemas: true, errors: true } }
        }, document.getElementById("asyncapi"));
      } catch (e) {
        fail();
      }
    })();
  </script>
</body>
</html>
"""


def _escape(text: str) -> str:
    """Minimal HTML-context escaping for the values interpolated into the page."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def asyncapi_docs_html(
    spec_url: str = "/api/asyncapi.yaml",
    *,
    title: str = "SmartLoad AsyncAPI",
) -> str:
    """Return a self-contained HTML page that renders the AsyncAPI spec at
    `spec_url` using the @asyncapi/react-component standalone bundle.

    `spec_url` is fetched by the browser at runtime (same-origin when served
    from the BFF), so the page never embeds the spec and never needs
    regenerating when the spec changes.
    """
    return (
        _ASYNCAPI_TEMPLATE
        .replace("__TITLE__", _escape(title))
        .replace("__CDN_BASE__", _CDN_BASE)
        .replace("__SPEC_URL_JS__", json.dumps(spec_url))
        .replace("__SPEC_URL__", _escape(spec_url))
    )
