"""Telemetry read endpoints. Pairs with services/telemetry."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import SmartLoadClient


class MetricsClient:
    """HTTP client for /api/v1/metrics."""

    def __init__(self, parent: "SmartLoadClient"):
        self._parent = parent

    def read(self, service: str, window: str = "5m") -> list[dict]:
        """Return recent metric rows for a service.

        Pending implementation (#127). Will issue:
            GET {base_url}/api/v1/metrics?service=<service>&window=<window>
        """
        raise NotImplementedError("Pending issue #127")
