#!/bin/bash
# Launcher — uses the python3.12 venv where duckdb is installed.
# Run daily via cron or manually.
VENV=/tmp/akam_venv312
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Recreate venv if missing (e.g. after reboot which clears /tmp)
if [ ! -f "$VENV/bin/python" ]; then
    echo "Recreating venv at $VENV..."
    python3.12 -m venv "$VENV"
    "$VENV/bin/pip" install duckdb requests --quiet
fi

exec "$VENV/bin/python" "$SCRIPT_DIR/run_all.py" "$@"
