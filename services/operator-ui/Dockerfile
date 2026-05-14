# Multi-stage build for the operator UI.
#
# Stage 1 — node: build the React app from web/
# Stage 2 — python: install the Flask BFF and copy the static build in.
#
# The BFF serves the static build at / and proxies /api/ui/* to upstream
# SmartLoad services. The image carries no node_modules and no build tools.
#
# Note on the OpenAPI spec: docs/openapi/smartload-v1.yaml lives outside this
# service folder. Rather than widen the build context, docker-compose mounts
# it at runtime as a read-only volume at /app/openapi/smartload-v1.yaml.

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

# Python deps
COPY bff/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# BFF code
COPY bff/ /app/bff/

# Built frontend
COPY --from=web-builder /web/dist /app/web/dist

# Placeholder so the directory exists; docker-compose mounts the real spec.
RUN mkdir -p /app/openapi

ENV PORT=8090 \
    WEB_DIST=/app/web/dist \
    OPENAPI_PATH=/app/openapi/smartload-v1.yaml \
    PYTHONUNBUFFERED=1

EXPOSE 8090

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8090", "bff.app:app"]
