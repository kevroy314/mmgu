#!/usr/bin/env bash
# GCE startup script: install Docker, fetch Hallkeeper, keep it running. Safe to run on every boot.
set -euo pipefail
if ! command -v docker >/dev/null; then
  apt-get update -y
  apt-get install -y ca-certificates curl git
  curl -fsSL https://get.docker.com | sh
fi
REPO="$(curl -fs -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/hallkeeper-repo || echo https://github.com/kevroy314/mmgu.git)"
if [ ! -d /opt/hallkeeper/.git ]; then
  git clone "$REPO" /opt/hallkeeper
fi
mkdir -p /opt/hallkeeper/data && chown -R 1000:1000 /opt/hallkeeper/data
if [ -f /opt/hallkeeper/.env ]; then
  cd /opt/hallkeeper && docker compose up -d --build
fi
