@echo off
cd /d "%~dp0"
echo Running KTrade DAILY scanner only, no VectorBT...
.venv\Scripts\python.exe ktrade_daily_runner.py daily --no-backtest
pause
