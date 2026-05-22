#!/bin/bash
# Starts the fleet manager server in the background using the project venv.

set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
VENV_ACTIVATE="$ROOT_DIR/.venv/bin/activate"
LOG_FILE="$ROOT_DIR/nohup.out"

PID=$(pgrep -f "python.*fleet_manager.server" || true)
if [ -n "$PID" ]; then
    echo "Fleet manager server already running (PID: $PID)"
    exit 0
fi

if [ ! -f "$VENV_ACTIVATE" ]; then
    echo "Error: virtualenv not found at $VENV_ACTIVATE"
    echo "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
    exit 1
fi

cd "$ROOT_DIR"

# Cron @reboot starts with a minimal PATH and does not source ~/.bashrc.
# Keep user-installed CLIs such as opencode available to launched sessions.
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"

if [ -f "$ROOT_DIR/.env" ]; then
    set -a
    source "$ROOT_DIR/.env"
    set +a
fi

source "$VENV_ACTIVATE"

nohup python -m fleet_manager.server >> "$LOG_FILE" 2>&1 &
PID=$!

echo "Started fleet manager server (PID: $PID)"
echo "Logging to: $LOG_FILE"
