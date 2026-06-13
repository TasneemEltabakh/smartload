"""
services/shared/metrics.py
──────────────────────────
Shared Prometheus instrumentation for SmartLoad services (#161).

Each service constructs one `ServiceMetrics(prefix)` at import time and serves
`metrics_response()` from a Flask `/metrics` route. The common surface is the
same across services so dashboards and alerts can template on the prefix:

    <prefix>_up                                  1 while the process is alive
    <prefix>_cycle_total{outcome}                run-loop cycles by outcome
    <prefix>_cycle_duration_seconds              run-loop cycle wall time (histogram)
    <prefix>_publish_total{channel,outcome}      envelopes published
    <prefix>_publish_duration_seconds            publish wall time (histogram)

Services add their own decision-distribution counters on top (rl action+mode,
anomaly isolate per backend, autoscaler scale direction+mechanism, …) using the
plain `prometheus_client` API.

prometheus_client keeps a per-process global registry, so each service process
owns its own metrics — there is no cross-service collision even though the
helper is shared code.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class ServiceMetrics:
    """The common metric surface every SmartLoad service exposes."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.up = Gauge(f"{prefix}_up", f"1 while the {prefix} process is alive")
        self.up.set(1)
        self.cycle_total = Counter(
            f"{prefix}_cycle_total", "Run-loop cycles by outcome", ["outcome"],
        )
        self.cycle_duration = Histogram(
            f"{prefix}_cycle_duration_seconds", "Run-loop cycle wall time (seconds)",
        )
        self.publish_total = Counter(
            f"{prefix}_publish_total", "Envelopes published by channel + outcome",
            ["channel", "outcome"],
        )
        self.publish_duration = Histogram(
            f"{prefix}_publish_duration_seconds", "Publish wall time (seconds)",
        )

    @contextmanager
    def time_cycle(self, outcome: str = "ok"):
        """Time one run-loop cycle and record `cycle_total{outcome}`.

        Yields a one-key dict so the caller can refine the outcome label, e.g.::

            with METRICS.time_cycle() as c:
                published = _inference_cycle(...)
                c["outcome"] = "published" if published else "idle"

        An exception escaping the block is recorded as `outcome="error"` and
        re-raised."""
        start = time.perf_counter()
        box = {"outcome": outcome}
        try:
            yield box
        except Exception:
            box["outcome"] = "error"
            raise
        finally:
            self.cycle_duration.observe(time.perf_counter() - start)
            self.cycle_total.labels(outcome=box["outcome"]).inc()

    @contextmanager
    def time_publish(self, channel: str):
        """Time a publish and record `publish_total{channel,outcome}` +
        `publish_duration_seconds`. Records `outcome="error"` on exception."""
        start = time.perf_counter()
        outcome = "ok"
        try:
            yield
        except Exception:
            outcome = "error"
            raise
        finally:
            self.publish_duration.observe(time.perf_counter() - start)
            self.publish_total.labels(channel=channel, outcome=outcome).inc()

    def record_publish(self, channel: str, outcome: str = "ok") -> None:
        """Count a publish without timing it (for already-instrumented paths)."""
        self.publish_total.labels(channel=channel, outcome=outcome).inc()


def metrics_response() -> tuple[bytes, str]:
    """`(body, content_type)` for a Flask `/metrics` route, e.g.::

        @app.route("/metrics")
        def metrics():
            body, ctype = metrics_response()
            return Response(body, mimetype=ctype)
    """
    return generate_latest(), CONTENT_TYPE_LATEST
