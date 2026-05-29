#!/usr/bin/env bash
# hermes-webui-hotrestart — 一键拉取更新 + 重启 WebUI
# 用法: webui-update  或  webui-restart
set -euo pipefail

REPO_ROOT="/home/ayaka/hermes-webui"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PID_FILE="$HERMES_HOME/webui.pid"
PORT="${HERMES_WEBUI_PORT:-8787}"
BRIDGE_SOCK="/tmp/hermes-webui-bridge.sock"
LOG_FILE="$HERMES_HOME/webui.log"

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${GREEN}[webui]${NC} $1"; }
warn()  { echo -e "${YELLOW}[webui]${NC} $1"; }
err()   { echo -e "${RED}[webui]${NC} $1"; }
step()  { echo -e "${CYAN}  →${NC} $1"; }

# ── 1. 停止现有进程 ──
stop_all() {
    info "Stopping WebUI..."

    # 用 ctl.sh 停止
    cd "$REPO_ROOT"
    ./ctl.sh stop 2>/dev/null || true

    # 强制杀死残留的 server.py
    local pids=$(pgrep -f "python.*hermes-webui/server.py" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        step "Killing stale server.py: $pids"
        kill $pids 2>/dev/null || true
        sleep 1
        kill -9 $pids 2>/dev/null || true
    fi

    # 停止 bridge
    pkill -f "agent_bridge.py" 2>/dev/null || true
    rm -f "$BRIDGE_SOCK"

    # 清理 PID 文件
    rm -f "$PID_FILE"

    info "Stopped"
}

# ── 2. 拉取最新代码 ──
pull_latest() {
    info "Fetching updates..."
    cd "$REPO_ROOT"

    # Stash 本地修改
    if ! git diff --quiet; then
        warn "Stashing local changes..."
        git stash --include-untracked -m "auto-stash before hotrestart" 2>/dev/null || true
    fi

    # 从上游拉取
    step "Fetching upstream (nesquena/hermes-webui)..."
    HTTPS_PROXY="${HTTPS_PROXY:-http://192.168.0.110:7897}" git fetch upstream --tags 2>/dev/null || true

    # 从 fork 拉取
    step "Fetching origin (your fork)..."
    HTTPS_PROXY="${HTTPS_PROXY:-http://192.168.0.110:7897}" git fetch origin 2>/dev/null || true

    # 合并上游 → 本地
    LOCAL=$(git rev-parse HEAD)
    UPSTREAM=$(git rev-parse upstream/master 2>/dev/null || echo "$LOCAL")
    if [ "$LOCAL" != "$UPSTREAM" ]; then
        step "Merging upstream/master → local..."
        git merge upstream/master --no-edit 2>/dev/null || {
            warn "Merge conflict! Aborting merge. Your local changes are stashed."
            git merge --abort 2>/dev/null || true
        }
    fi

    # 合并 fork
    ORIGIN=$(git rev-parse origin/master 2>/dev/null || echo "$LOCAL")
    if [ "$(git rev-parse HEAD)" != "$ORIGIN" ]; then
        step "Pulling origin/master..."
        HTTPS_PROXY="${HTTPS_PROXY:-http://192.168.0.110:7897}" git pull origin master --rebase 2>/dev/null || true
    fi

    HEAD=$(git rev-parse --short HEAD)
    info "At commit: $HEAD"
}

# ── 3. 一次性迁移 (SQLite) ──
run_migrations() {
    if [ "${HERMES_WEBUI_SQLITE:-}" = "1" ]; then
        step "Checking SQLite migration..."
        cd "$REPO_ROOT"
        python3 -c "
from api.session_store_sqlite import SQLITE_ENABLED, migrate_from_json, get_stats
if SQLITE_ENABLED:
    stats = get_stats()
    if stats.get('total_sessions', 0) == 0:
        from api.config import SESSION_DIR
        result = migrate_from_json(str(SESSION_DIR))
        print(f'  SQLite migration: {result[\"migrated\"]} sessions')
    else:
        print(f'  SQLite: {stats[\"total_sessions\"]} sessions already migrated')
" 2>/dev/null || true
    fi
}

# ── 4. 启动 Bridge ──
start_bridge() {
    if [ "${HERMES_WEBUI_BRIDGE:-}" = "1" ]; then
        step "Starting Agent Bridge..."
        cd "$REPO_ROOT"
        nohup python3 api/agent_bridge.py > /dev/null 2>&1 &
        # 等 bridge 就绪
        for i in $(seq 1 15); do
            if [ -S "$BRIDGE_SOCK" ]; then
                info "Bridge ready"
                return 0
            fi
            sleep 0.3
        done
        warn "Bridge may not be ready (socket not found)"
    fi
}

# ── 5. 启动 WebUI ──
start_webui() {
    info "Starting WebUI on port $PORT..."
    cd "$REPO_ROOT"

    # 加载 .env
    if [ -f .env ]; then
        set -a; source .env; set +a
    fi

    # 用 ctl.sh 启动（后台守护）
    ./ctl.sh start

    # 等它起来
    for i in $(seq 1 20); do
        if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" 2>/dev/null | grep -q 200; then
            info "WebUI running at http://localhost:$PORT"
            return 0
        fi
        sleep 0.5
    done
    warn "WebUI started but health check failed — check $LOG_FILE"
}

# ── 主流程 ──
main() {
    echo ""
    info "╔════════════════════════════════════════╗"
    info "║   Hermes WebUI Hot Restart            ║"
    info "╚════════════════════════════════════════╝"
    echo ""

    stop_all
    pull_latest
    run_migrations
    start_bridge
    start_webui

    echo ""
    info "Done! http://localhost:$PORT"
    echo ""

    # 显示状态
    step "Status:"
    echo "  Port:    $PORT"
    echo "  Bridge:  $([ -S "$BRIDGE_SOCK" ] && echo 'enabled ✓' || echo 'disabled')"
    echo "  SQLite:  ${HERMES_WEBUI_SQLITE:-0}"
    echo "  Commit:  $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    echo "  Log:     $LOG_FILE"
}

main "$@"
