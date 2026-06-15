# Discord Accept Bot - Windows PowerShell starter
# Dubbelklik of run:  .\start.ps1

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "==> Discord Accept Bot starter" -ForegroundColor Cyan

# Check virtual env
$venvActivate = Join-Path $scriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    Write-Host "Activating virtual environment..." -ForegroundColor DarkGray
    & $venvActivate
} else {
    Write-Host "Geen .venv gevonden. Maak er een aan met:" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor Yellow
    Write-Host "  pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path ".env")) {
    Write-Host ".env bestand niet gevonden!" -ForegroundColor Red
    Write-Host "Kopieer .env.example naar .env en vul je DISCORD_TOKEN in." -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting bot..." -ForegroundColor Green
python bot.py
