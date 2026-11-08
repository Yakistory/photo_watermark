#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

export PATH="$HOME/.local/bin:$PATH"

EXPOSE_PORT=$(awk -F '[ =]+' '/^expose_port/ {gsub(/[^0-9]/, "", $2); print $2; exit}' .preview 2>/dev/null || echo 5000)

# Kill existing process on the port (never touch 9000)
if [ "$EXPOSE_PORT" != "9000" ]; then
  fuser -k "${EXPOSE_PORT}/tcp" 2>/dev/null || true
  sleep 1
fi

source .venv/bin/activate
export FLASK_PORT="$EXPOSE_PORT"
exec python app.py
