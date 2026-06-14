FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in the canonical shared/
# module alongside autoscaler/.
COPY autoscaler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY autoscaler/app.py            /app/app.py
COPY autoscaler/cluster_client.py /app/cluster_client.py
COPY autoscaler/decisions.py      /app/decisions.py
COPY autoscaler/controllers.py    /app/controllers.py
COPY autoscaler/manual.py         /app/manual.py
COPY shared                       /app/shared

ENV PORT=8085
ENV SERVICE_NAME=autoscaler

EXPOSE 8085

CMD ["python", "app.py"]
