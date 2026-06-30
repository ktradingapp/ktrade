@echo off
cd /d "%~dp0"

REM v13.2: clean app-data dir so EmergencyController SQLite state never leaks between runs
set KTRADE_APP_DATA_DIR=%CD%\.test_appdata
if exist "%KTRADE_APP_DATA_DIR%" rmdir /s /q "%KTRADE_APP_DATA_DIR%"
mkdir "%KTRADE_APP_DATA_DIR%"
set KTRADE_DB_PATH=

.venv\Scripts\python.exe -m py_compile agent\ktrade_agent_v9.py
.venv\Scripts\python.exe scripts\check_release_safety.py

.venv\Scripts\python.exe test_v103_fixes.py
.venv\Scripts\python.exe test_v105_sector_cap.py
.venv\Scripts\python.exe test_v106_fixes.py
.venv\Scripts\python.exe test_v107_fixes.py
.venv\Scripts\python.exe test_v108_sqlite.py
.venv\Scripts\python.exe test_v109_earnings.py
.venv\Scripts\python.exe test_v111_promotion.py
.venv\Scripts\python.exe test_v112_strategy_switch.py
.venv\Scripts\python.exe test_v113_safety_spine.py
.venv\Scripts\python.exe test_v125_review_fixes.py
.venv\Scripts\python.exe test_v126_production_fixes.py
.venv\Scripts\python.exe test_v128_resilience.py
.venv\Scripts\python.exe test_v129_whipsaw.py
.venv\Scripts\python.exe test_v130_copilot.py
.venv\Scripts\python.exe test_v131_copilot_analysis.py
.venv\Scripts\python.exe test_v132_regime.py
.venv\Scripts\python.exe data\test_price_sanity.py

pause
