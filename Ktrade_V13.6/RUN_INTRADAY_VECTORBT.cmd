@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe ktrade_intraday_vectorbt.py --fast --universe extended
pause
