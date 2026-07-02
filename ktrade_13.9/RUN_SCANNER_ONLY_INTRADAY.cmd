@echo off
cd /d "%~dp0"
echo Running KTrade INTRADAY scanner only, no VectorBT...
.venv\Scripts\python.exe ktrade_daily_runner.py intraday --no-backtest
pause
