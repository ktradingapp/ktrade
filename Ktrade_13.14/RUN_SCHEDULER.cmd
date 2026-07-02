@echo off
cd /d "%~dp0"
echo Starting KTrade backtest scheduler (always-on)...
echo Daily backtest after close, intraday before open, trading days only.
.venv\Scripts\python.exe ktrade_scheduler.py
pause
