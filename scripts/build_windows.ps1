$ErrorActionPreference = "Stop"

if (-not (Test-Path .venv)) {
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
& .\.venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --name "UNNAMED-Operator" desktop_main.py
Write-Host "Build created under .\dist\UNNAMED-Operator\"
