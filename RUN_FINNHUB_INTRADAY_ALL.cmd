@echo off
cd /d "%~dp0"
echo Running KTrade FINNHUB INTRADAY scan + intraday VectorBT backtest...
set KTRADE_DATA_PROVIDER=finnhub
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_INTRADAY_SCAN_INTERVAL=5m
.venv\Scripts\python.exe ktrade_daily_runner.py intraday
pause
