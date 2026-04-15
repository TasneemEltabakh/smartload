# OpenTelemetry Collector with Prometheus receiver
# contrib version includes prometheus receiver

FROM otel/opentelemetry-collector-contrib:0.104.0

# Copy configuration
COPY collector-config.yaml /etc/otel-collector-config.yaml

# Health check endpoint (default /metrics)
HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:13133 || exit 1

# Collector starts with config
CMD ["--config=/etc/otel-collector-config.yaml"]