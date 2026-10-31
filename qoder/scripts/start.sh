#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

PYTHON_APP="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_APP" ]; then
  echo '请先运行 bash scripts/setup.sh。' >&2
  exit 1
fi
export DATA_DIR="${DATA_DIR:-$PROJECT_DIR/data}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-4323}"
mkdir -p "$DATA_DIR"
echo "组会接力：http://$HOST:$PORT；数据目录：$DATA_DIR"
exec "$PYTHON_APP" -m uvicorn app.main:app --app-dir "$PROJECT_DIR" --host "$HOST" --port "$PORT" --workers 1
