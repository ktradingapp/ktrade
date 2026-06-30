KTrade v10.3 - Daily Run Guide
==============================

Project Folder
--------------
C:\trading-agent\Ktrade_v10_3

Main Dashboard URL
------------------
http://localhost:5001

Important Note
--------------
For normal daily use, you only need:
1. Backend / Dashboard server
2. Scanner

VectorBT is NOT required every day. Use VectorBT only when you want to refresh or retest strategy/backtest parameters.


1) Start Backend / Dashboard Server
-----------------------------------
Open CMD window 1 and run:

cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe backend\ktrade_alpaca.py

Use:
- Starts the local dashboard server.
- Connects to Alpaca paper account.
- Handles positions, account, orders, buy/sell paper orders.
- Handles broker reconciliation.
- Keeps the web app running at http://localhost:5001

Keep this CMD window open while using the dashboard.


2) Open Dashboard
-----------------
Open browser and go to:

http://localhost:5001

Use:
- Shows signals, positions, trades, alerts, performance, AI advisor.
- Shows Alpaca paper account information.
- Shows Broker OK / REVIEW reconciliation status.


3) Run Daily / Swing Scanner
----------------------------
Open CMD window 2 and run:

cd /d C:\trading-agent\Ktrade_v10_3
set KTRADE_DATA_PROVIDER=yfinance
set KTRADE_SCAN_SYMBOLS=
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_SCAN_INTERVAL=1d
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only

Use:
- Scans stocks using daily candles.
- Best for daily / swing / momentum signals.
- Updates dashboard signals.
- Does not place orders automatically.
- Saves result here:
  C:\trading-agent\Ktrade_v10_3\data\ktrade_scan_latest.json

Meaning of settings:
- KTRADE_DATA_PROVIDER=yfinance means use yfinance as data source.
- KTRADE_SCAN_SYMBOLS= blank means use default/extended universe.
- KTRADE_SCAN_UNIVERSE=extended means scan your full extended stock list.
- KTRADE_SCAN_INTERVAL=1d means daily candles.
- --score-only means scanner only creates signals and does not trade.


4) Run Intraday Scanner
-----------------------
Use this only when you want intraday signals.

cd /d C:\trading-agent\Ktrade_v10_3
set KTRADE_DATA_PROVIDER=yfinance
set KTRADE_SCAN_SYMBOLS=
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_SCAN_INTERVAL=5m
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only

Use:
- Scans stocks using 5-minute candles.
- Best for intraday trade checking.
- Updates dashboard with intraday-style signals.
- Does not place orders automatically.

Simple rule:
- 1d = daily / swing / momentum style
- 5m = intraday style


5) Run Tests
------------
Optional. Run when you want to confirm project health.

cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe test_v103_fixes.py
.venv\Scripts\python.exe data\test_price_sanity.py

Use:
- Checks v10.3 safety fixes.
- Checks bad-price / decimal-shift protection.
- Checks risk, strategy, execution behavior.

Expected result:
- v10.3 tests should show passed.
- Price sanity tests should show ALL PRICE-SANITY TESTS PASSED.


6) Run VectorBT Backtest
------------------------
Not needed every day.
Run only when you want to refresh backtest/approved parameters.

cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe ktrade_vectorbt.py

Use:
- Runs historical backtest.
- Updates approved strategy parameters.
- Helps check whether strategy worked historically.


7) Run Intraday VectorBT Backtest
---------------------------------
Optional. Use when you want intraday strategy backtest.

cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe ktrade_intraday_vectorbt.py --fast --universe extended

Use:
- Runs intraday VectorBT backtest.
- Creates intraday approved parameters/report.


Daily Minimum Commands
----------------------
CMD 1:

cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe backend\ktrade_alpaca.py

CMD 2:

cd /d C:\trading-agent\Ktrade_v10_3
set KTRADE_DATA_PROVIDER=yfinance
set KTRADE_SCAN_SYMBOLS=
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_SCAN_INTERVAL=1d
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only

Then open:

http://localhost:5001


Easy Double-Click Files
-----------------------
These files are also available in this folder:

START_BACKEND.cmd
RUN_SCANNER_DAILY.cmd
RUN_SCANNER_INTRADAY.cmd
RUN_VECTORBT.cmd
RUN_INTRADAY_VECTORBT.cmd
RUN_TESTS.cmd

You can double-click them instead of typing commands.


Which Files Are Actually Run?
-----------------------------
Main runtime files:
- backend\ktrade_alpaca.py
- agent\ktrade_agent_v9.py
- frontend\KTrade_preview.html

Support files used automatically:
- backend\broker_reconciler.py
- agent\broker_adapter.py
- data\ktrade_data.py
- data\price_sanity.py
- risk\ktrade_risk.py

Backtest files:
- ktrade_vectorbt.py
- ktrade_intraday_vectorbt.py

Test files:
- test_v103_fixes.py
- data\test_price_sanity.py


Safety Notes
------------
- --score-only does not place orders.
- Paper orders are controlled by KTRADE_PAPER_ORDER_SUBMISSION in .env.
- This project is for Alpaca PAPER trading/testing, not live money trading.
- Broker reconciliation checks dashboard state against Alpaca broker truth.
- Bad price guard blocks decimal-shift/bad tick problems before trade approval.
