# Multi-stage build for the demo UI (developer end-to-end validation harness).
#
# Stage 1 — node: build the React app from web/
# Stage 2 — python: install the Flask BFF and copy the static build in.

# ── stage 1: frontend ─────────────────────────────────────────────────────────
FROM node:20-alpine AS web-builder
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build


# ── stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

COPY bff/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bff/ /app/bff/
COPY --from=web-builder /web/dist /app/web/dist

ENV PORT=8091 \
    WEB_DIST=/app/web/dist \
    PYTHONUNBUFFERED=1

EXPOSE 8091

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8091", "bff.app:app"]
