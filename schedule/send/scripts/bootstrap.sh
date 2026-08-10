#!/usr/bin/env bash
# Bootstrap project-local .venv. Never installs into system Python.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  echo "[bootstrap] creating .venv at $ROOT/.venv"
  python3 -m venv .venv
fi

# Always use the venv pip explicitly — never `pip install` on system.
"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -r requirements.txt

echo "[bootstrap] done. Activate with: source .venv/bin/activate"
