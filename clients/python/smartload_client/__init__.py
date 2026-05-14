"""SmartLoad Python client."""

from .client import SmartLoadClient
from .events import (
    CHANNEL_ANOMALY,
    CHANNEL_FORECAST,
    CHANNEL_POLICY,
    CHANNEL_ROUTING,
    CHANNEL_SCALE,
    PolicySubscription,
)
from .exceptions import (
    AuthenticationError,
    RateLimitError,
    SmartLoadError,
    ValidationError,
)

__all__ = [
    "SmartLoadClient",
    "PolicySubscription",
    "SmartLoadError",
    "AuthenticationError",
    "ValidationError",
    "RateLimitError",
    "CHANNEL_POLICY",
    "CHANNEL_ANOMALY",
    "CHANNEL_FORECAST",
    "CHANNEL_ROUTING",
    "CHANNEL_SCALE",
]

__version__ = "0.1.0"
