# KTrade PRO - SAFE scanner-only runner (v10.6)
# NOTE: real paper execution goes through agent + broker_adapter, NOT a separate
# trader script. The old ktrade_alpaca_trader.py does not exist. This script only
# runs the read-only score scan. To place paper orders, wire the broker adapter
# and set KTRADE_PAPER_ORDER_SUBMISSION=true deliberately.
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project

Write-Host "[KTrade] Scanning universe (score-only, no orders)..." -ForegroundColor Cyan
$env:KTRADE_DATA_PROVIDER = "yfinance"
& "$Project\.venv\Scripts\python.exe" "$Project\agent\ktrade_agent_v9.py" --score-only

Write-Host "`n[KTrade] Done. This script does NOT place trades." -ForegroundColor Green
Write-Host "  For a single REAL data cycle (still no orders unless submission is enabled):" -ForegroundColor Yellow
Write-Host "    .venv\Scripts\python.exe agent\ktrade_agent_v9.py --once" -ForegroundColor Yellow
