# KTrade PRO - One full cycle (scan + trade)
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project

$env:POLYGON_KEY = ""
Write-Host "[KTrade] Single cycle | yfinance" -ForegroundColor Cyan
& "$Project\.venv\Scripts\python.exe" "$Project\agent\ktrade_agent_v9.py" --once
