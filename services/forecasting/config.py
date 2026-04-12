import os
from dataclasses import dataclass


@dataclass
class Config:
    service_name: str
    port: int
    history_file: str
    redis_host: str
    redis_port: int
    redis_channel: str
    smoothing_alpha: float
    horizon_minutes: int
    minimum_history_points: int
    acceptable_mape: float
    enable_scheduler: bool
    forecast_interval_seconds: int


def _to_bool(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def load_config():
    return Config(
        service_name=os.getenv("SERVICE_NAME", "forecasting"),
        port=int(os.getenv("PORT", "8080")),
        history_file=os.getenv(
            "HISTORY_FILE",
            os.path.join(os.path.dirname(__file__), "data", "request_history.json")
        ),
        redis_host=os.getenv("REDIS_HOST", "localhost"),
        redis_port=int(os.getenv("REDIS_PORT", "6379")),
        redis_channel=os.getenv("REDIS_CHANNEL", "smartload.forecast.load"),
        smoothing_alpha=float(os.getenv("SMOOTHING_ALPHA", "0.4")),
        horizon_minutes=int(os.getenv("HORIZON_MINUTES", "5")),
        minimum_history_points=int(os.getenv("MINIMUM_HISTORY_POINTS", "20")),
        acceptable_mape=float(os.getenv("ACCEPTABLE_MAPE", "18.0")),
        enable_scheduler=_to_bool(os.getenv("ENABLE_SCHEDULER", "false")),
        forecast_interval_seconds=int(os.getenv("FORECAST_INTERVAL_SECONDS", "300"))
    )
