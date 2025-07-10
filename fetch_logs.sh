#!/usr/bin/env bash
#
# fetch_logs.sh — pull /data/logs from a balena container down to ./logs
#
# Usage: ./fetch_logs.sh <DEVICE> <SERVICE_NAME> [LOCAL_DIR]

set -euo pipefail

DEVICE="$1"
SERVICE="$2"
LOCAL_DIR="${3:-./logs}"

# 1) Ensure local dir exists
mkdir -p "$LOCAL_DIR"

# 2) Find a free ephemeral port for the tunnel
pick_port() {
  while :; do
    p=$((RANDOM % 20000 + 20000))
    ! lsof -i TCP:"$p" &>/dev/null && echo "$p" && return
  done
}
TUNNEL_PORT=$(pick_port)
echo "🔌 Opening balena tunnel on localhost:${TUNNEL_PORT} → ${DEVICE}:22222"

# 3) Avoid campus HTTP proxy for localhost traffic
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy

# 4) Launch the tunnel in background, capture any errors
LOGFILE=$(mktemp)
balena device tunnel "$DEVICE" -p 22222:"$TUNNEL_PORT" >"$LOGFILE" 2>&1 &
TUN_PID=$!

# Ensure we kill tunnel on exit
cleanup(){ kill "$TUN_PID" 2>/dev/null || true; rm -f "$LOGFILE"; }
trap cleanup EXIT

# 5) Wait up to 10s for the local port to listen
echo "⏳ Waiting for tunnel to bind on port ${TUNNEL_PORT}..."
for i in $(seq 1 10); do
  if (echo > /dev/tcp/localhost/"$TUNNEL_PORT") &>/dev/null; then
    echo "✅ Tunnel is up!"
    break
  fi
  sleep 1
done

if ! (echo > /dev/tcp/localhost/"$TUNNEL_PORT") &>/dev/null; then
  echo "❌ Tunnel never bound on port ${TUNNEL_PORT}."
  echo "--- Tunnel logs ---"
  cat "$LOGFILE"
  exit 1
fi

# 6) Find your container ID
CID=$(ssh -o StrictHostKeyChecking=no -p "$TUNNEL_PORT" root@localhost \
      "balena-engine ps -q -f name=$SERVICE")

if [[ -z "$CID" ]]; then
  echo "❌ No container found matching service '$SERVICE'."
  exit 1
fi

# 7) Stream and extract /data/logs
echo "📦 Streaming /data/logs from container ${CID}..."
ssh -p "$TUNNEL_PORT" root@localhost \
  "balena-engine exec -i $CID tar czf - -C /data logs" \
  | tar xz -C "$LOCAL_DIR"

echo "✅ Logs downloaded to ${LOCAL_DIR}/logs"
