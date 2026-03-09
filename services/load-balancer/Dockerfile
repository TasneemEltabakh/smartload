FROM python:3.11-slim

WORKDIR /app

COPY app.py .

ENV PORT=8080
ENV SERVICE_NAME=load-balancer

EXPOSE 8080

CMD ["python", "app.py"]