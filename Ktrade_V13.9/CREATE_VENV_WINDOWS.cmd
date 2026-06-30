@echo off
setlocal
cd /d %~dp0
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Done. Virtual environment created in .venv
echo Next: edit .env and add your API keys.
pause
