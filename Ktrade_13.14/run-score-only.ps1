# KTrade PRO - Score-only scan
# Uses ktrade_agent_v9.py (has built-in yfinance fallback)
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project

$env:POLYGON_KEY = ""
Write-Host "[KTrade] Score-only | yfinance | no trades" -ForegroundColor Cyan
& "$Project\.venv\Scripts\python.exe" "$Project\agent\ktrade_agent_v9.py" --score-only
