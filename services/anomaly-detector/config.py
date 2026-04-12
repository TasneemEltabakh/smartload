import os
from dataclasses import dataclass


@dataclass
class Config:
    service_name: str
    port: int
    telemetry_file: str
    redis_host: str
    redis_port: int
    redis_channel: str
    ewma_alpha: float
    warning_z_score: float
    critical_z_score: float
    warning_error_rate: float
    critical_error_rate: float
    warning_latency_floor_ms: float
    critical_latency_floor_ms: float
    stale_after_seconds: int
    minimum_history_points: int


def load_config():
    return Config(
        service_name=os.getenv("SERVICE_NAME", "anomaly-detector"),
        port=int(os.getenv("PORT", "8080")),
        telemetry_file=os.getenv(
            "TELEMETRY_FILE",
            os.path.join(os.path.dirname(__file__), "data", "telemetry_stream.json")
        ),
        redis_host=os.getenv("REDIS_HOST", "localhost"),
        redis_port=int(os.getenv("REDIS_PORT", "6379")),
        redis_channel=os.getenv("REDIS_CHANNEL", "smartload.anomaly.status"),
        ewma_alpha=float(os.getenv("EWMA_ALPHA", "0.35")),
        warning_z_score=float(os.getenv("WARNING_Z_SCORE", "2.0")),
        critical_z_score=float(os.getenv("CRITICAL_Z_SCORE", "3.0")),
        warning_error_rate=float(os.getenv("WARNING_ERROR_RATE", "0.10")),
        critical_error_rate=float(os.getenv("CRITICAL_ERROR_RATE", "0.20")),
        warning_latency_floor_ms=float(os.getenv("WARNING_LATENCY_FLOOR_MS", "250")),
        critical_latency_floor_ms=float(os.getenv("CRITICAL_LATENCY_FLOOR_MS", "450")),
        stale_after_seconds=int(os.getenv("STALE_AFTER_SECONDS", "120")),
        minimum_history_points=int(os.getenv("MINIMUM_HISTORY_POINTS", "5"))
    )
