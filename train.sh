#!/bin/bash
set -e

echo "Starting Train Dispatcher App..."

sudo ufw disable

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "Missing venv Python at $PYTHON"
    echo "Create it with: python3 -m venv .venv"
    exit 1
fi

"$PYTHON" "$SCRIPT_DIR/service_manager.py"
