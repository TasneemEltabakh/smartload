# Pre-built locust image for the adaptive-advantage benchmarks.
#
# Replaces the per-side `pip install --quiet locust` inside a bare python:3.11-slim
# (run.sh) — that cost ~20-40s on every benchmark side AND was a network-flakiness
# source. Pinned to the version the prior runs used so behaviour is byte-identical.
#
# The locustfile is bind-mounted at /locust by run.sh at runtime (NOT COPYed here),
# so editing locustfile.py never requires a rebuild.
#
# Build once:  docker build -t smartload-locust:latest experiments/adaptive-advantage/locust
# (run.sh auto-builds it if the image is missing.)
FROM python:3.11-slim
RUN pip install --no-cache-dir locust==2.44.3
