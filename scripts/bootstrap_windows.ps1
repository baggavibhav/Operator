$ErrorActionPreference = "Stop"

Write-Host "UNNAMED Operator V0.5 bootstrap"
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
    Write-Warning "Ollama was not found. Model-planned and research-agent tasks need Ollama; deterministic local file tasks can still run."
} else {
    $ModelInstalled = ollama list 2>$null | Select-String -Pattern "^phi4-mini(?::latest)?\s"
    if (-not $ModelInstalled) {
        Write-Host "Pulling Phi-4 Mini local development model..."
        ollama pull phi4-mini
    } else {
        Write-Host "Phi-4 Mini development model already installed."
    }
}

Write-Host "Starting UNNAMED Operator V0.5..."
& $Python .\desktop_main.py
