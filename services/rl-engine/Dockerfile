FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8084
ENV SERVICE_NAME=rl-engine

EXPOSE 8084

CMD ["python", "app.py"]