@echo off
cd /d "%~dp0"
set KTRADE_BIND_HOST=127.0.0.1
set KTRADE_PORT=5001
set KTRADE_PAPER_ORDER_SUBMISSION=false
set LIVE_TRADING=false
set KTRADE_MANUAL_ALLOW_DEMO=false
.venv\Scripts\python.exe scripts\run_ktrade_local.py --open-browser
pause
