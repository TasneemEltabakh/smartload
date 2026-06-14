FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in the canonical shared/
# module alongside anomaly-detector/. Mirrors the policy-manager layout.
COPY anomaly-detector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY anomaly-detector/app.py          /app/app.py
COPY anomaly-detector/manual.py       /app/manual.py
COPY anomaly-detector/runloop.py      /app/runloop.py
COPY anomaly-detector/engine_base.py  /app/engine_base.py
COPY anomaly-detector/engines         /app/engines
# Per-backend temporal feature extraction shared by the trend_rule (default)
# and trend_forest engines. Without this the trend engines can't import
# features.trend, raise on construction, and the run loop silently falls back
# to threshold — so this COPY is load-bearing for the default engine.
COPY anomaly-detector/features        /app/features
# Ship the trained Isolation Forest bundle so ANOMALY_ENGINE=isolation_forest
# can actually load it in the container — without this the engine silently
# falls back to threshold (the model was shipped-but-unused). See issue #165.
COPY anomaly-detector/models          /app/models
COPY shared                           /app/shared

ENV PORT=8082
ENV SERVICE_NAME=anomaly-detector

EXPOSE 8082

CMD ["python", "app.py"]