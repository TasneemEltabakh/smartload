FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

ENV NGINX_LOG_PATH=/nginx-logs/access.log
ENV OTEL_COLLECTOR_URL=http://otel-collector:4318/v1/metrics
ENV SERVICE_NAME=load-balancer

CMD ["python", "-u", "app.py"]
