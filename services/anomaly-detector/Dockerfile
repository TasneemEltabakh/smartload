FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in the canonical shared/
# module alongside anomaly-detector/. Mirrors the policy-manager layout.
COPY anomaly-detector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY anomaly-detector/app.py          /app/app.py
COPY anomaly-detector/runloop.py      /app/runloop.py
COPY anomaly-detector/engine_base.py  /app/engine_base.py
COPY anomaly-detector/engines         /app/engines
COPY shared                           /app/shared

ENV PORT=8082
ENV SERVICE_NAME=anomaly-detector

EXPOSE 8082

CMD ["python", "app.py"]