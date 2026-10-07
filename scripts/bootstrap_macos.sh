#!/usr/bin/env bash
set -euo pipefail

echo "UNNAMED Operator V0.5 bootstrap"
echo "Development build: installs runtime dependencies into a local .venv only."

command -v python3 >/dev/null 2>&1 || { echo "Python 3.11+ is required for this development build. The final installer will bundle its runtime."; exit 1; }

if [[ ! -d .venv ]]; then
  echo "Creating local virtual environment..."
  python3 -m venv .venv
fi

PYTHON=".venv/bin/python"
MARKER=".venv/.operator_requirements.sha256"
if command -v shasum >/dev/null 2>&1; then
  CURRENT_HASH="$(shasum -a 256 requirements-desktop.txt | awk '{print $1}')"
else
  CURRENT_HASH="$(openssl dgst -sha256 requirements-desktop.txt | awk '{print $NF}')"
fi
INSTALLED_HASH="$(cat "$MARKER" 2>/dev/null || true)"

if [[ "$CURRENT_HASH" != "$INSTALLED_HASH" ]]; then
  echo "Installing/updating desktop dependencies..."
  "$PYTHON" -m pip install --upgrade pip
  "$PYTHON" -m pip install -r requirements-desktop.txt
  printf '%s' "$CURRENT_HASH" > "$MARKER"
else
  echo "Desktop dependencies already match this build."
fi

if command -v ollama >/dev/null 2>&1; then
  if ! ollama list 2>/dev/null | grep -Eq '^phi4-mini(:latest)?[[:space:]]'; then
    echo "Pulling Phi-4 Mini local development model..."
    ollama pull phi4-mini
  else
    echo "Phi-4 Mini development model already installed."
  fi
else
  echo "Warning: Ollama was not found. Model-planned and research-agent tasks need Ollama; deterministic local file tasks can still run."
fi

echo "Starting UNNAMED Operator V0.5..."
"$PYTHON" desktop_main.py
