# KTrade PRO - Start the Alpaca paper trading bridge
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project

# Check if already running
$connection = Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction SilentlyContinue
if ($connection) {
    Write-Host "[KTrade] Bridge already running on port 5001." -ForegroundColor Yellow
    exit 0
}

# Credentials check
if (-not $env:ALPACA_KEY) {
    Write-Host "[KTrade] WARNING: ALPACA_KEY not set - running in demo mode" -ForegroundColor Yellow
    Write-Host "  Get free keys at https://alpaca.markets" -ForegroundColor Gray
}

Write-Host "[KTrade] Starting paper trading bridge on http://localhost:5001 ..." -ForegroundColor Cyan
& "$Project\.venv\Scripts\python.exe" "$Project\backend\ktrade_alpaca.py"
