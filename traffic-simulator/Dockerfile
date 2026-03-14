FROM python:3.11-slim

WORKDIR /app

RUN pip install locust

COPY locustfile.py .

CMD ["locust", "-f", "locustfile.py", "--host=http://nginx"]