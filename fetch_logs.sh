#!/usr/bin/env bash
#
# fetch_logs.sh — pull /data/logs from a balena container down to ./logs
#
# Usage: ./fetch_logs.sh <DEVICE> <SERVICE_NAME> [LOCAL_DIR]

set -euo pipefail

DEVICE="$1"
SERVICE="$2"
LOCAL_DIR="${3:-./logs}"

# 1) Make sure local output dir exists
mkdir -p "$LOCAL_DIR"

# 2) Open a tunnel: map remote port 22222 -> local port 7000
#    (balenaOS host SSH always listens on 22222) :contentReference[oaicite:0]{index=0}
TUNNEL_PORT=7000
balena device tunnel "$DEVICE" -p 22222:$TUNNEL_PORT &
TUN_PID=$!

# Give the tunnel a moment to come up
sleep 2

# 3) Find the container ID for our service on the host OS
CID=$(ssh -o StrictHostKeyChecking=no -p $TUNNEL_PORT root@localhost \
      "balena-engine ps -q -f name=$SERVICE")

if [[ -z "$CID" ]]; then
  echo "❌ No container found matching service '$SERVICE'."
  kill $TUN_PID
  exit 1
fi

# 4) Exec into the container and tar up /data/logs → stdout, then untar locally
ssh -p $TUNNEL_PORT root@localhost \
  "balena-engine exec -i $CID tar czf - -C /data logs" \
  | tar xz -C "$LOCAL_DIR"

echo "✅ Logs downloaded to ${LOCAL_DIR}/logs"

# 5) Tear down the tunnel
kill $TUN_PID
