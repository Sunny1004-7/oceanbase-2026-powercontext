#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
tools="${R12_TOOLS_DIR:-$PWD/.tools}"
mkdir -p "$tools"
if [[ "$(uname -m)" != x86_64 ]]; then echo 'This fixed setup targets Linux x86_64.'; exit 1; fi
if [[ ! -x "$tools/node-v22.14.0-linux-x64/bin/node" ]]; then
  curl -fL --retry 2 https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz -o "$tools/node.tar.xz"
  curl -fL --retry 2 https://nodejs.org/dist/v22.14.0/SHASUMS256.txt -o "$tools/SHASUMS256.txt"
  expected="$(awk '$2=="node-v22.14.0-linux-x64.tar.xz" {print $1}' "$tools/SHASUMS256.txt")"
  [[ -n "$expected" ]]
  printf '%s  %s\n' "$expected" "$tools/node.tar.xz" | sha256sum -c -
  tar -xf "$tools/node.tar.xz" -C "$tools"
fi
export PATH="$tools/node-v22.14.0-linux-x64/bin:$PATH"
command -v node; node --version; command -v npm; npm --version
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm ci --ignore-scripts --legacy-peer-deps --include=optional
.venv/bin/python scripts/fetch_model.py
echo "Ready. PATH='$tools/node-v22.14.0-linux-x64/bin:\$PATH' .venv/bin/python scripts/run.py"
