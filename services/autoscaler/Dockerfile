FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8085
ENV SERVICE_NAME=autoscaler

EXPOSE 8085

CMD ["python", "app.py"]