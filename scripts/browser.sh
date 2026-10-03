#!/usr/bin/env bash
# Run a headless Chromium (official Playwright image, pinned by digest) as a browser
# server on 127.0.0.1:3123, for e2e tests and screenshots without installing Chromium's
# system libraries locally. Stop it with: docker rm -f wp-browser
#   PLAYWRIGHT_WS_ENDPOINT=ws://127.0.0.1:3123/ uv run pytest -m e2e
set -euo pipefail
IMAGE="mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27"
docker rm -f wp-browser >/dev/null 2>&1 || true
docker run -d --rm --name wp-browser --network host --init --ipc=host "$IMAGE" \
  /bin/sh -c "npx -y playwright@1.63.0 run-server --port 3123 --host 127.0.0.1" >/dev/null
for _ in $(seq 1 60); do
  if docker logs wp-browser 2>&1 | grep -q Listening; then echo "browser ready: ws://127.0.0.1:3123/"; exit 0; fi
  sleep 1
done
echo "browser did not start" >&2; docker logs wp-browser >&2; exit 1
