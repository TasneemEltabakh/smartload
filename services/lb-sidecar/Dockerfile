FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in shared/ alongside lb-sidecar/.
COPY lb-sidecar/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY lb-sidecar/app.py     /app/app.py
COPY lb-sidecar/runloop.py /app/runloop.py
COPY shared                /app/shared

ENV PORT=8087
ENV SERVICE_NAME=lb-sidecar

EXPOSE 8087

CMD ["python", "app.py"]
