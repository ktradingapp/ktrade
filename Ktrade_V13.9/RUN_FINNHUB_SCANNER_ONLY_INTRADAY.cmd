@echo off
cd /d "%~dp0"
echo Running KTrade FINNHUB INTRADAY scanner only, no VectorBT...
set KTRADE_DATA_PROVIDER=finnhub
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_INTRADAY_SCAN_INTERVAL=5m
.venv\Scripts\python.exe ktrade_daily_runner.py intraday --no-backtest
pause
