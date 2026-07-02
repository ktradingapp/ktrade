@echo off
cd /d "%~dp0"
echo Downloading FinBERT model. This may take several minutes.
.venv\Scripts\python.exe download_finbert_model.py
pause
