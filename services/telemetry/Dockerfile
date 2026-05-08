FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in the canonical shared module.
COPY telemetry/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telemetry/app.py /app/app.py
COPY shared           /app/shared

ENV PORT=8081
ENV SERVICE_NAME=telemetry

EXPOSE 8081

CMD ["python", "app.py"]
