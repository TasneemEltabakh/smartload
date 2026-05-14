"""Typed exceptions surfaced by the SmartLoad client."""

from __future__ import annotations


class SmartLoadError(Exception):
    """Base class for all client-side errors."""


class AuthenticationError(SmartLoadError):
    """Raised when the API key is missing, invalid, or revoked."""


class ValidationError(SmartLoadError):
    """Raised when the server rejects the request body (HTTP 400).

    The `field` attribute names the offending field when the server
    pinpoints it (the SmartLoad policy-manager echoes `{"field": "..."}`
    in its 400 responses).
    """

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class RateLimitError(SmartLoadError):
    """Raised when the server returns HTTP 429.

    The `retry_after` attribute (seconds) is populated from the
    Retry-After header when present.
    """

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
