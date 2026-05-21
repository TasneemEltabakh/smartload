FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in the canonical shared/
# module alongside rl-engine/. Mirrors the anomaly-detector / forecasting
# layout.
COPY rl-engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rl-engine/app.py          /app/app.py
COPY rl-engine/runloop.py      /app/runloop.py
COPY rl-engine/policy_base.py  /app/policy_base.py
COPY rl-engine/policies        /app/policies
COPY shared                    /app/shared

ENV PORT=8084
ENV SERVICE_NAME=rl-engine

EXPOSE 8084

CMD ["python", "app.py"]