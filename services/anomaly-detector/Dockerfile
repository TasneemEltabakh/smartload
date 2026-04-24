FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8082
ENV SERVICE_NAME=anomaly-detector

EXPOSE 8082

CMD ["python", "app.py"]