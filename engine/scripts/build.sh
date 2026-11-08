#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

export PATH="$HOME/.local/bin:$PATH"
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
