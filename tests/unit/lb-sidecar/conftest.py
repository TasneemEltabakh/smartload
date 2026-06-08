"""
tests/unit/lb-sidecar/conftest.py
─────────────────────────────────
Shared fixtures for lb-sidecar unit tests.

The autouse `_stub_dns_resolution` fixture patches socket.gethostbyname
to always succeed. Unit tests use synthetic backend names like
`b1:8080` / `a:8080` that don't resolve via the real resolver; the
NginxAdapter's #155 Risk 3 pre-flight (`socket.gethostbyname()` before
nginx -s reload) would otherwise defer every reload and the existing
assertions (conf written, reload triggered) would fail. Production
defaults to dns_preflight=True; the stub here lets the tests keep
using synthetic names without per-call overrides.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _stub_dns_resolution():
    """Make socket.gethostbyname always return a fake IP for unit tests.

    The NginxAdapter pre-flight calls this; in unit tests we need it to
    succeed so the reload path actuates. Real DNS resolution still
    happens in integration / production paths."""
    with patch.object(socket, "gethostbyname", return_value="127.0.0.1"):
        yield
