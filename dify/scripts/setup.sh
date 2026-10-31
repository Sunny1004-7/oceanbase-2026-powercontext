#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/python" -m pip install -r "$PROJECT_DIR/requirements.lock.txt"
"$PROJECT_DIR/.venv/bin/python" -m pip check
"$PROJECT_DIR/.venv/bin/python" "$SCRIPT_DIR/setup_adapter.py"
echo "Local config is ready at $PROJECT_DIR/.env.local"
