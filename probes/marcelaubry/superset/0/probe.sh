#!/usr/bin/env bash
# Verifier smoke probe (issue 0 = not a remediation case). Proves that a real Superset
# frontend Jest test can be installed and executed inside the credential-free runner at the
# pinned base SHA: exit 0 when the toolchain, dependency install and Jest work end to end.
# The "head" exit code is never expected; readiness runs this probe against BASE only.
set -euo pipefail
cd superset-frontend
export CI=1 NODE_OPTIONS="--max-old-space-size=3072"
./node_modules/.bin/jest --ci --runInBand --silent src/utils/findPermission.test.ts
