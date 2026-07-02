# KTrade PRO - Extended scan (full universe)
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project

$env:POLYGON_KEY = ""
$env:KTRADE_SCAN_UNIVERSE = "extended"
Write-Host "[KTrade] Extended scan | yfinance | full universe" -ForegroundColor Cyan
& "$Project\.venv\Scripts\python.exe" "$Project\agent\ktrade_agent_v9.py" --score-only
