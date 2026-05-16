# Build context must be ./services (same as telemetry) so we can COPY shared/.
# docker build -t smartload-forecasting -f services/forecasting/Dockerfile ./services

FROM python:3.11-slim

# Prophet (cmdstanpy) requires a C++ toolchain to compile CmdStan at install time
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential wget && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY forecasting/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/                   /app/shared/
COPY forecasting/app.py        /app/app.py
COPY forecasting/forecaster.py /app/forecaster.py
# Pre-trained model artifact — present after running train.py offline.
# The service falls back to live-fit or moving average if the file is absent.
COPY forecasting/models/       /app/models/

ENV PORT=8083
ENV SERVICE_NAME=forecasting

EXPOSE 8083

CMD ["python", "app.py"]
