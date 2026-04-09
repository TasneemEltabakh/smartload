FROM python:3.11-slim

# Install Docker CLI to read container logs
RUN apt-get update && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV METRICS_PORT=9113
ENV SERVICE_NAME=nginx-lb
ENV INSTANCE_ID=nginx-001
ENV NODE_ID=a0000000-0000-0000-0000-000000000001
ENV SOURCE=real
ENV ENVIRONMENT=development
ENV NGINX_CONTAINER_NAME=infrastructure-nginx-1

EXPOSE 9113

CMD ["python", "app.py"]