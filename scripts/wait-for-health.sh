#!/usr/bin/env bash
# Polls the app /health endpoint until it returns 200 or times out.
# Used by `make up` so the target blocks until the stack is actually ready.
set -euo pipefail

URL="${APP_HEALTH_URL:-http://localhost:8001/health}"
TIMEOUT="${WAIT_TIMEOUT_SECONDS:-60}"
INTERVAL="${WAIT_INTERVAL_SECONDS:-1}"

elapsed=0
while (( elapsed < TIMEOUT )); do
    if curl -sf "$URL" >/dev/null 2>&1; then
        echo "[wait-for-health] $URL is healthy"
        exit 0
    fi
    sleep "$INTERVAL"
    elapsed=$(( elapsed + INTERVAL ))
done

echo "[wait-for-health] timeout after ${TIMEOUT}s waiting for $URL" >&2
exit 1
