"""Webhook management endpoints. Pairs with services/webhook-dispatcher (#130)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import SmartLoadClient


class WebhooksClient:
    """HTTP client for /api/v1/webhooks."""

    def __init__(self, parent: "SmartLoadClient"):
        self._parent = parent

    def register(self, url: str, events: list[str], secret: str) -> dict:
        """Register a webhook subscription.

        Pending implementation (#130).
        """
        raise NotImplementedError("Pending issue #130")

    def list(self) -> list[dict]:
        raise NotImplementedError("Pending issue #130")

    def unregister(self, webhook_id: str) -> None:
        raise NotImplementedError("Pending issue #130")
