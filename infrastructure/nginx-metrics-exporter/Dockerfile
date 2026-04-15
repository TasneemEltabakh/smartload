FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV METRICS_PORT=9113
ENV SERVICE_NAME=nginx-lb
ENV INSTANCE_ID=nginx-001
ENV NODE_ID=a0000000-0000-0000-0000-000000000001
ENV SOURCE=real
ENV ENVIRONMENT=development

EXPOSE 9113

CMD ["python", "-u", "app.py"]