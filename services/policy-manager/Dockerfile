FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in the canonical shared/
# module alongside policy-manager/. Mirrors the autoscaler layout.
COPY policy-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY policy-manager/app.py        /app/app.py
COPY policy-manager/validation.py /app/validation.py
COPY shared                       /app/shared

ENV PORT=8086
ENV SERVICE_NAME=policy-manager

EXPOSE 8086

CMD ["python", "app.py"]
