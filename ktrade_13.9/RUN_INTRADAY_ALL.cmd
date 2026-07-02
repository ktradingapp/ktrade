@echo off
cd /d "%~dp0"
echo Running KTrade INTRADAY scan + intraday VectorBT backtest...
.venv\Scripts\python.exe ktrade_daily_runner.py intraday
pause
