"""SmartLoad Python client."""

from .client import SmartLoadClient
from .engines import EngineChannel, EngineService, EnginesSubscription
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
from .status import (
    ActivePolicySnapshot,
    RecentEvents,
    ServiceStatus,
    StatusResponse,
)

__all__ = [
    "SmartLoadClient",
    "PolicySubscription",
    "EnginesSubscription",
    "EngineChannel",
    "EngineService",
    "SmartLoadError",
    "AuthenticationError",
    "ValidationError",
    "RateLimitError",
    "CHANNEL_POLICY",
    "CHANNEL_ANOMALY",
    "CHANNEL_FORECAST",
    "CHANNEL_ROUTING",
    "CHANNEL_SCALE",
    "StatusResponse",
    "ServiceStatus",
    "ActivePolicySnapshot",
    "RecentEvents",
]

__version__ = "0.1.0"
