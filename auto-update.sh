#!/usr/bin/env bash
# Hermes WebUI weekly auto-update & restart
# Runs via cron, stops webui, pulls latest from fork, restarts with bridge.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"

# ── 1. Fetch latest from fork ──
cd "$REPO_ROOT"
HTTPS_PROXY=http://192.168.0.110:7897 git fetch origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "[webui-updater] Already up to date ($LOCAL)"
    exit 0
fi

echo "[webui-updater] Update available: $LOCAL → $REMOTE"

# ── 2. Stop webui ──
./ctl.sh stop 2>/dev/null || true
sleep 2

# Kill any remaining bridge process
pkill -f "agent_bridge.py" 2>/dev/null || true
# Clean up stale socket
rm -f /tmp/hermes-webui-bridge.sock

# ── 3. Pull + restart ──
HTTPS_PROXY=http://192.168.0.110:7897 git pull origin master --rebase
echo "[webui-updater] Pulled, restarting..."

# Start bridge first
nohup python api/agent_bridge.py > /dev/null 2>&1 &

# Wait for bridge to be ready
for i in $(seq 1 10); do
    if [ -S /tmp/hermes-webui-bridge.sock ]; then
        echo "[webui-updater] Bridge ready"
        break
    fi
    sleep 0.5
done

# Start webui
./ctl.sh start

echo "[webui-updater] Restart complete, now at $(git rev-parse --short HEAD)"
