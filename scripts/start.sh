#!/usr/bin/env bash
# Multi-Agent Studio - Start / Stop Services (Linux/macOS)
# Usage:
#   bash scripts/start.sh          # Start services
#   bash scripts/start.sh stop     # Stop services

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_ROOT/logs"
PID_DIR="$PROJECT_ROOT/.pid"
PORTS=(8000 3000)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Helper: find processes listening on a port (returns PIDs)
# ---------------------------------------------------------------------------
port_pids() {
    local port=$1
    # ss is preferred; fall back to lsof
    if command -v ss &>/dev/null; then
        ss -tlnp "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u || true
    elif command -v lsof &>/dev/null; then
        lsof -ti :$port 2>/dev/null | sort -u || true
    fi
}

# ---------------------------------------------------------------------------
# Helper: kill a PID and all its descendants
# ---------------------------------------------------------------------------
kill_tree() {
    local pid=$1
    if ! kill -0 "$pid" 2>/dev/null; then
        return
    fi

    # Collect child PIDs recursively
    local pids=()
    local queue=("$pid")
    local visited=()
    while [[ ${#queue[@]} -gt 0 ]]; do
        local current="${queue[0]}"
        queue=("${queue[@]:1}")
        for v in "${visited[@]+"${visited[@]}"}"; do
            [[ "$v" == "$current" ]] && continue 2
        done
        visited+=("$current")
        if kill -0 "$current" 2>/dev/null; then
            pids+=("$current")
        fi
        # Find children
        local children
        children=$(ps -o pid= --ppid "$current" 2>/dev/null | tr -d ' ' || true)
        for child in $children; do
            queue+=("$child")
        done
    done

    # Kill in reverse order (children first, then parent)
    for (( i=${#pids[@]}-1; i>=0; i-- )); do
        kill "${pids[$i]}" 2>/dev/null || true
    done
}

# ---------------------------------------------------------------------------
# Helper: kill all processes on a given port
# ---------------------------------------------------------------------------
kill_port() {
    local port=$1
    local pids
    pids=$(port_pids "$port")
    if [[ -z "$pids" ]]; then
        echo -e "  Port ${port}: no processes found ${GRAY}"
        return 0
    fi

    for pid in $pids; do
        local name
        name=$(ps -o comm= -p "$pid" 2>/dev/null || echo "unknown")
        echo -e "  Port ${port}: killing ${name}[${pid}] ${YELLOW}"
        kill_tree "$pid"
    done
}

# ---------------------------------------------------------------------------
# Helper: wait until a port is free
# ---------------------------------------------------------------------------
wait_port_free() {
    local port=$1
    local timeout=${2:-10}
    local elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        if [[ -z "$(port_pids "$port")" ]]; then
            echo -e "  Port ${port}: free ${GREEN}"
            return 0
        fi
        sleep 0.5
        elapsed=$((elapsed + 1))
    done
    echo -e "  [WARN] Port ${port} still occupied after ${timeout}s ${RED}"
    return 1
}

# ---------------------------------------------------------------------------
# Stop logic
# ---------------------------------------------------------------------------
stop_services() {
    echo -e "${CYAN}=== Stopping Multi-Agent Studio ===${NC}"

    # Step 1: Kill via PID files
    for pf in backend.pid frontend.pid; do
        local pidfile="$PID_DIR/$pf"
        if [[ -f "$pidfile" ]]; then
            local pid
            pid=$(cat "$pidfile")
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                echo -e "  Stopping ${pf/.pid/} (PID=${pid}) ${YELLOW}"
                kill_tree "$pid"
            fi
            rm -f "$pidfile"
        fi
    done

    sleep 0.5

    # Step 2: Kill any remaining processes on our ports
    for port in "${PORTS[@]}"; do
        kill_port "$port"
    done

    sleep 0.5

    # Step 3: Kill any remaining matching python/node processes
    local py_pids
    py_pids=$(pgrep -f "app\.main|uvicorn|watchfiles.*orchestrator" 2>/dev/null || true)
    for pid in $py_pids; do
        echo -e "  Killing python PID=${pid} ${YELLOW}"
        kill_tree "$pid"
    done

    local node_pids
    node_pids=$(pgrep -f "next.*apps/web|next-server.*apps/web" 2>/dev/null || true)
    for pid in $node_pids; do
        echo -e "  Killing node PID=${pid} ${YELLOW}"
        kill_tree "$pid"
    done

    # Step 4: Wait for ports to be free
    echo -e "  Waiting for ports to be released... ${GRAY}"
    for port in "${PORTS[@]}"; do
        wait_port_free "$port" 8
    done

    # Final status
    echo ""
    local clean=true
    for port in "${PORTS[@]}"; do
        if [[ -n "$(port_pids "$port")" ]]; then
            echo -e "  [WARN] Port ${port} still occupied ${RED}"
            clean=false
        fi
    done

    if $clean; then
        echo -e "${GREEN}All services stopped.${NC}"
    else
        echo -e "${YELLOW}Some ports still occupied. Run 'bash scripts/start.sh stop' again.${NC}"
    fi
}

# ---------------------------------------------------------------------------
# Start logic
# ---------------------------------------------------------------------------
start_services() {
    mkdir -p "$LOG_DIR" "$PID_DIR"

    # Check dependencies
    if ! command -v poetry &>/dev/null; then
        echo -e "${RED}[ERROR] poetry not found${NC}"
        exit 1
    fi

    echo -e "${CYAN}=== Multi-Agent Studio ===${NC}"
    echo ""

    # Kill any existing processes on our ports
    echo -e "  Cleaning up old processes... ${GRAY}"
    for port in "${PORTS[@]}"; do
        kill_port "$port"
    done

    # Wait until ports are actually free
    echo -e "  Waiting for ports to be released... ${GRAY}"
    for port in "${PORTS[@]}"; do
        wait_port_free "$port" 10
    done

    echo ""

    # Start backend
    echo -e "  Starting backend (port 8000)... ${GRAY}"
    local backend_log="$LOG_DIR/orchestrator.log"
    (
        cd "$PROJECT_ROOT/apps/orchestrator"
        nohup setsid poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info > "$backend_log" 2>&1 &
        echo $! > "$PID_DIR/backend.pid"
    )
    local backend_pid
    backend_pid=$(cat "$PID_DIR/backend.pid")
    echo -e "    PID=${backend_pid} ${GRAY}"

    # Start frontend
    echo -e "  Starting frontend (port 3000)... ${GRAY}"
    local frontend_log="$LOG_DIR/frontend.log"
    (
        cd "$PROJECT_ROOT/apps/web"
        if command -v pnpm &>/dev/null; then
            nohup setsid pnpm dev > "$frontend_log" 2>&1 &
        else
            nohup setsid npm run dev > "$frontend_log" 2>&1 &
        fi
        echo $! > "$PID_DIR/frontend.pid"
    )
    local frontend_pid
    frontend_pid=$(cat "$PID_DIR/frontend.pid")
    echo -e "    PID=${frontend_pid} ${GRAY}"

    echo ""

    # Quick health check
    sleep 3

    local health
    # Get LAN IP for display
    local lan_ip
    lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "0.0.0.0")

    health=$(curl -sf http://localhost:8000/health 2>/dev/null || echo "")
    if [[ "$health" == *"ok"* ]]; then
        echo -e "  Backend:  OK  http://${lan_ip}:8000 ${GREEN}"
    else
        echo -e "  Backend:  starting... see logs/orchestrator.log ${YELLOW}"
    fi

    local fe_code
    fe_code=$(curl -sf -o /dev/null -w "%{http_code}" "http://localhost:3000" 2>/dev/null || echo "000")
    if [[ "$fe_code" =~ ^(200|307)$ ]]; then
        echo -e "  Frontend: OK  http://${lan_ip}:3000 ${GREEN}"
    else
        echo -e "  Frontend: starting... see logs/frontend.log ${YELLOW}"
    fi

    echo ""
    echo -e "Logs: ${LOG_DIR}/ ${GRAY}"
    echo -e "Stop: bash scripts/start.sh stop ${GRAY}"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
ACTION="${1:-start}"
if [[ "$ACTION" == "stop" ]]; then
    stop_services
else
    start_services
fi
