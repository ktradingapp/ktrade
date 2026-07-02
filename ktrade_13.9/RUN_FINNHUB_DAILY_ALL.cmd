@echo off
cd /d "%~dp0"
echo Running KTrade FINNHUB DAILY scan + daily VectorBT backtest...
set KTRADE_DATA_PROVIDER=finnhub
set KTRADE_SCAN_UNIVERSE=extended
.venv\Scripts\python.exe ktrade_daily_runner.py daily
pause
