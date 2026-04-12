FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

ENV PORT=8080
ENV SERVICE_NAME=anomaly-detector

EXPOSE 8080

CMD ["python", "app.py"]
