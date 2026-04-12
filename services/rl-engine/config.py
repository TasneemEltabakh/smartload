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
    max_latency_ms: float
    anomaly_penalty_threshold: float
    latency_weight: float
    error_weight: float
    cpu_weight: float
    memory_weight: float
    load_weight: float


def load_config():
    return Config(
        service_name=os.getenv("SERVICE_NAME", "rl-engine"),
        port=int(os.getenv("PORT", "8080")),
        telemetry_file=os.getenv(
            "TELEMETRY_FILE",
            os.path.join(os.path.dirname(__file__), "data", "routing_metrics.json")
        ),
        redis_host=os.getenv("REDIS_HOST", "localhost"),
        redis_port=int(os.getenv("REDIS_PORT", "6379")),
        redis_channel=os.getenv("REDIS_CHANNEL", "smartload.routing.scores"),
        max_latency_ms=float(os.getenv("MAX_LATENCY_MS", "1000")),
        anomaly_penalty_threshold=float(os.getenv("ANOMALY_PENALTY_THRESHOLD", "0.05")),
        latency_weight=float(os.getenv("LATENCY_WEIGHT", "0.30")),
        error_weight=float(os.getenv("ERROR_WEIGHT", "0.30")),
        cpu_weight=float(os.getenv("CPU_WEIGHT", "0.15")),
        memory_weight=float(os.getenv("MEMORY_WEIGHT", "0.10")),
        load_weight=float(os.getenv("LOAD_WEIGHT", "0.15"))
    )
