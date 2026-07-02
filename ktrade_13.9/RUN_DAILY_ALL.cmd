@echo off
cd /d "%~dp0"
echo Running KTrade DAILY scan + daily VectorBT backtest...
.venv\Scripts\python.exe ktrade_daily_runner.py daily
pause
