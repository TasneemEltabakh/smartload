FROM python:3.11-slim

WORKDIR /app

# Build context is ./services so we can pull in the canonical shared/
# module alongside rl-engine/. Mirrors the anomaly-detector / forecasting
# layout.
COPY rl-engine/requirements.txt .

# Install CPU-only PyTorch first (SB3 depends on it; avoids pulling the
# CUDA-enabled default wheel which adds ~2 GB to the image).
RUN pip install --no-cache-dir \
    torch --extra-index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt

# ── serving-path files only — training/ is deliberately excluded ──────────────
# The training/ directory (env.py, dataset.py, train_ppo.py, etc.) must NEVER
# be copied here. The CI runtime-import-smoke job enforces this: it builds this
# image and asserts that `import app; import policies.ppo.policy` does NOT cause
# any training.* module to appear in sys.modules.
COPY rl-engine/app.py          /app/app.py
COPY rl-engine/runloop.py      /app/runloop.py
COPY rl-engine/policy_base.py  /app/policy_base.py
COPY rl-engine/obs_builder.py  /app/obs_builder.py
COPY rl-engine/policies        /app/policies
COPY rl-engine/models          /app/models
COPY shared                    /app/shared

ENV PORT=8084
ENV SERVICE_NAME=rl-engine

EXPOSE 8084

CMD ["python", "app.py"]
