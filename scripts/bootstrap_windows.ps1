$ErrorActionPreference = "Stop"

Write-Host "UNNAMED Operator V0.4 bootstrap"
Write-Host "Development build: installs runtime dependencies into a local .venv only."

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.11+ is required for this development build. The final installer will bundle its runtime."
}

if (-not (Test-Path .venv)) {
    Write-Host "Creating local virtual environment..."
    python -m venv .venv
}

$Python = ".\.venv\Scripts\python.exe"
$Marker = ".\.venv\.operator_requirements.sha256"
$CurrentHash = (Get-FileHash .\requirements-desktop.txt -Algorithm SHA256).Hash
$InstalledHash = if (Test-Path $Marker) { (Get-Content $Marker -Raw).Trim() } else { "" }

if ($InstalledHash -ne $CurrentHash) {
    Write-Host "Installing/updating desktop dependencies..."
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r requirements-desktop.txt
    Set-Content -Path $Marker -Value $CurrentHash -Encoding ASCII
} else {
    Write-Host "Desktop dependencies already match this build."
}

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Warning "Ollama was not found. Install/start Ollama for model-planned tasks. Grounded deterministic file tasks can run without model planning."
} else {
    $ModelInstalled = ollama list 2>$null | Select-String -Pattern "^qwen2\.5:1\.5b\s"
    if (-not $ModelInstalled) {
        Write-Host "Pulling local development model..."
        ollama pull qwen2.5:1.5b
    } else {
        Write-Host "Local development model already installed."
    }
}

Write-Host "Starting UNNAMED Operator V0.4..."
& $Python .\desktop_main.py
