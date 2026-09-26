# NeuralGateway 1-Click Launch Script
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Starting NeuralGateway Distributed Core  " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$VENV_PYTHON = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $VENV_PYTHON)) {
    Write-Host "[!] Virtual environment not detected. Creating..." -ForegroundColor Yellow
    python -m venv .venv
    & ".\.venv\Scripts\pip" install -r requirements.txt
}

Write-Host "[*] Launching ASGI Worker on http://127.0.0.1:8000..." -ForegroundColor Green
Write-Host "[*] Interactive Docs: http://127.0.0.1:8000/docs" -ForegroundColor Green
Write-Host "[*] Metrics Scrape:   http://127.0.0.1:8000/metrics" -ForegroundColor Green
Write-Host "[*] Health Check:     http://127.0.0.1:8000/healthz" -ForegroundColor Green

& $VENV_PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
