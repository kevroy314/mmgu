#!/usr/bin/env bash
# Create a small Google Compute Engine VM that runs Hallkeeper with Docker Compose.
#
# Usage:  PROJECT=my-gcp-project ./deploy/gcp/create-vm.sh
# Then:   copy your .env (and optionally your data/ folder) to the VM, see docs/hosting.md.
#
# Cost (us-central1, 2026 list prices, check the calculator): e2-small ≈ $13/month + 20 GB disk ≈ $1/month.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to your GCP project id}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-hallkeeper}"
MACHINE="${MACHINE:-e2-small}"
DISK_GB="${DISK_GB:-20}"
REPO="${REPO:-https://github.com/kevroy314/mmgu.git}"

gcloud config set project "$PROJECT" >/dev/null
gcloud services enable compute.googleapis.com >/dev/null

gcloud compute instances create "$NAME" \
  --zone "$ZONE" \
  --machine-type "$MACHINE" \
  --image-family debian-12 --image-project debian-cloud \
  --boot-disk-size "${DISK_GB}GB" --boot-disk-type pd-balanced \
  --tags hallkeeper \
  --metadata "hallkeeper-repo=$REPO" \
  --metadata-from-file startup-script="$(dirname "$0")/startup.sh"

cat <<MSG

VM "$NAME" is starting. In a few minutes:
  gcloud compute scp .env "$NAME":/opt/hallkeeper/.env --zone "$ZONE"
  gcloud compute ssh "$NAME" --zone "$ZONE" -- 'cd /opt/hallkeeper && sudo docker compose up -d'

The hall listens only on the VM itself (127.0.0.1:8420). Put it on the internet with ONE of:
  - Cloudflare Tunnel (no open ports):   see docs/hosting.md#cloudflare-tunnel
  - Caddy with automatic HTTPS:           see docs/hosting.md#caddy
MSG
