#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
pyinstaller --noconfirm --clean --windowed --name "UNNAMED-Operator" desktop_main.py
echo "Build created under ./dist/UNNAMED-Operator.app"
