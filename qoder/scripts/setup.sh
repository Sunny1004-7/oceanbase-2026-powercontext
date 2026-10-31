#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 12), "需要 Python 3.12 或更高版本"'
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.lock.txt"
"$PROJECT_DIR/.venv/bin/python" -m pip check
"$PROJECT_DIR/.venv/bin/python" -c 'import importlib.metadata as m; print({k:m.version(k) for k in ("powercontext","fastapi","uvicorn","pydantic")})'
echo '安装完成。运行 bash scripts/start.sh 启动组会接力。'
