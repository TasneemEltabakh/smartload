FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in the canonical shared/
# module alongside forecasting/. Mirrors the anomaly-detector layout.
COPY forecasting/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forecasting/app.py          /app/app.py
COPY forecasting/runloop.py      /app/runloop.py
COPY forecasting/engine_base.py  /app/engine_base.py
COPY forecasting/engines         /app/engines
COPY shared                      /app/shared

ENV PORT=8083
ENV SERVICE_NAME=forecasting

EXPOSE 8083

CMD ["python", "app.py"]