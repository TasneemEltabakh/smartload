# Multi-stage build for the operator UI.
#
# Stage 1 — node: build the React app from operator-ui/web/
# Stage 2 — python: install the Flask BFF and copy the static build in.
#
# The BFF serves the static build at / and proxies /api/ui/* to upstream
# SmartLoad services. The image carries no node_modules and no build tools.
#
# Build context is ./services (widened in compose for #121 Live Engines) so
# this Dockerfile can pull in services/shared/ alongside operator-ui/ —
# matches the layout of every AI service. Without the widened context the
# BFF can't import shared.contracts.parse_envelope for the engines stream.
#
# Note on the OpenAPI spec: docs/openapi/smartload-v1.yaml lives outside the
# services/ tree. Rather than widen the build context further, docker-compose
# mounts it at runtime as a read-only volume at /app/openapi/smartload-v1.yaml.

# ── stage 1: frontend ─────────────────────────────────────────────────────────
FROM node:20-alpine AS web-builder
WORKDIR /web
COPY operator-ui/web/package.json operator-ui/web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY operator-ui/web/ ./
RUN npm run build


# ── stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Python deps
COPY operator-ui/bff/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# BFF code + shared library (parse_envelope for the engines stream)
COPY operator-ui/bff/ /app/bff/
COPY shared           /app/shared

# Built frontend
COPY --from=web-builder /web/dist /app/web/dist

# Placeholder so the directory exists; docker-compose mounts the real spec.
RUN mkdir -p /app/openapi

ENV PORT=8090 \
    WEB_DIST=/app/web/dist \
    OPENAPI_PATH=/app/openapi/smartload-v1.yaml \
    PYTHONUNBUFFERED=1

EXPOSE 8090

# gthread worker class so an SSE connection (long-lived) doesn't block other
# requests on the same worker. Two workers × eight threads each = up to 16
# concurrent SSE connections + the usual short-lived requests.
CMD ["gunicorn", "-k", "gthread", "-w", "2", "--threads", "8", "-b", "0.0.0.0:8090", "bff.app:app"]
