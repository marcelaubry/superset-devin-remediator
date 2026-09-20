# Credential-free probe verifier. Build context is the repository root:
#   docker build -f docker/verifier/Dockerfile .
# Nothing here receives an env_file; the image never contains .env (see .dockerignore) and the
# service runs as a dedicated non-root UID on a read-only root filesystem (docker-compose.yml).
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git bash ca-certificates curl nodejs npm \
 && npm install -g yarn@1.22.22 \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev
COPY remediator remediator
# UID/GID 65534 (nobody) owns nothing else on the host; RLIMIT_NPROC and the /proc marker
# sweep in remediator.probes.runner rely on the probe UID running nothing but probes.
RUN chown -R root:root /app && chmod -R a-w /app
USER 65534:65534
ENV PATH="/app/.venv/bin:$PATH" \
    VERIFIER_WORKSPACE_ROOT=/tmp \
    VERIFIER_PORT=8080
EXPOSE 8080
CMD ["python", "-m", "remediator.verifier"]
