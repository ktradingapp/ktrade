@echo off
cd /d "%~dp0"
set KTRADE_DATA_PROVIDER=finnhub
set KTRADE_SCAN_SYMBOLS=
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_SCAN_INTERVAL=5m
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only
pause
