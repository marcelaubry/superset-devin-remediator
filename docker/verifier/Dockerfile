# Credential-free probe verifier. Build context is the repository root:
#   docker build -f docker/verifier/Dockerfile .
# Nothing here receives an env_file or build argument carrying a secret; the image never
# contains .env (see .dockerignore) and the service runs as a dedicated non-root UID on a
# read-only root filesystem (docker-compose.yml). The only runtime secret (the verifier HMAC
# key) is delivered at start-up through VERIFIER_HMAC_KEY_FILE and never baked into a layer.
#
# Toolchain pinned to the Superset checkout this deployment verifies
# (superset-frontend/package.json engines: node ^24.16.0, npm ^11.13.0; .nvmrc v24.16.0);
# npm follows the newest 11.x so its bundled node-tar/undici carry the current CVE fixes.
#
# Multi-architecture: the Node tarball is chosen from BuildKit's TARGETARCH (amd64 -> Node
# "x64", arm64 -> Node "arm64") so the binary always matches the python:3.11-slim base of
# the same platform. Any other architecture fails the build instead of producing an image
# whose node cannot exec. Each tarball is verified against the SHA-256 pinned below, copied
# from https://nodejs.org/dist/v24.16.0/SHASUMS256.txt; bumping NODE_VERSION requires
# updating both digests (tests/test_phase6_canary.py pins the version and both digests).
ARG NODE_VERSION=24.16.0
ARG NODE_SHA256_X64=d804845d34eddc21dc1092b519d643ef40b1f58ec5dec5c22b1f4bd8fabde6c9
ARG NODE_SHA256_ARM64=524659219d6a207a7400f2bde15d19ba060ffbe0d32a8643319ad67e3bb64c78
ARG NPM_VERSION=11.19.1
ARG YARN_VERSION=1.22.22

FROM python:3.11-slim AS node-fetch
ARG TARGETARCH
ARG NODE_VERSION
ARG NODE_SHA256_X64
ARG NODE_SHA256_ARM64
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /node
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "${arch}" in \
      amd64) node_arch=x64;   node_sha256="${NODE_SHA256_X64}" ;; \
      arm64) node_arch=arm64; node_sha256="${NODE_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture '${arch}': only amd64 and arm64 have a pinned Node ${NODE_VERSION} artifact" >&2; exit 1 ;; \
    esac; \
    tarball="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}"; \
    echo "${node_sha256}  ${tarball}" | sha256sum -c -; \
    mkdir -p /opt/node; \
    tar -xJf "${tarball}" -C /opt/node --strip-components=1; \
    rm -f "${tarball}"; \
    /opt/node/bin/node --version | grep -qx "v${NODE_VERSION}"

FROM python:3.11-slim
ARG NPM_VERSION
ARG YARN_VERSION
# git/bash: clone + probe shell; build-essential/python3: native npm modules (node-gyp);
# ca-certificates: https clone/registry; no curl/wget at runtime (probes get no download tool).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git bash ca-certificates make g++ python3 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node-fetch /opt/node /opt/node
ENV PATH="/opt/node/bin:/app/.venv/bin:$PATH"
# Probes run with a fixed minimal PATH (/usr/local/bin:/usr/bin:/bin), so the pinned
# toolchain is linked there rather than found through an inherited PATH.
RUN npm install -g "npm@${NPM_VERSION}" "yarn@${YARN_VERSION}" \
 && npm cache clean --force \
 && rm -rf /root/.npm /root/.cache \
 && ln -s /opt/node/bin/node /opt/node/bin/npm /opt/node/bin/npx /opt/node/bin/yarn \
          /opt/node/bin/corepack /usr/local/bin/
# setuptools/wheel are only build-time helpers of the base image; the verifier venv is
# populated by uv from the lockfile, so drop them (and their vendored copies) from the image.
RUN pip install --no-cache-dir uv && pip uninstall -y setuptools wheel
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev
COPY remediator remediator
# UID/GID 65534 (nobody) owns nothing else on the host; RLIMIT_NPROC and the /proc marker
# sweep in remediator.probes.runner rely on the probe UID running nothing but probes.
# The same image also serves the key-holding front (UID 65533) and the egress proxy
# (UID 65532) with different entrypoints; only the runner gets /workspace.
RUN chown -R root:root /app /opt/node && chmod -R a-w /app /opt/node \
 && mkdir /workspace && chown 65534:65534 /workspace && chmod 700 /workspace
USER 65534:65534
ENV VERIFIER_WORKSPACE_ROOT=/tmp \
    VERIFIER_PROBE_ROOT=/probes \
    VERIFIER_PORT=8080 \
    HOME=/tmp \
    npm_config_cache=/tmp/npm-cache \
    YARN_CACHE_FOLDER=/tmp/yarn-cache
EXPOSE 8080
CMD ["python", "-m", "remediator.verifier"]
