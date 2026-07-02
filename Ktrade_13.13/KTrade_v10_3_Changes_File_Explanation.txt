KTrade v10.3 Changes and File Explanation
=========================================

Project Folder
--------------
C:\trading-agent\Ktrade_v10_3

Purpose Of This Document
------------------------
This document explains:
- What changed in v10.3
- Which files are new
- Which files were updated
- What each important file is used for
- What existed in the older version before v10.3


High-Level Summary
------------------
Before v10.3, the project already had:
- Dashboard UI
- Alpaca paper backend
- Scanner / signal engine
- Daily and intraday scanner support
- VectorBT backtesting
- Basic broker reconciliation
- Paper trade placement

v10.3 adds stronger safety and better trading correctness:
- Broker-truth reconciliation from real Alpaca fills
- Verified closed trades from Alpaca fill activity
- Bad-price / decimal-shift protection
- Safer execution: record fills only after broker confirms fill
- Broker adapter for paper bracket orders
- Better RiskEngine tracking of real positions/equity
- Improved scanner/strategy confirmation logic
- VectorBT look-ahead prevention
- Market-hours and holiday awareness


Main v10.3 Improvements
-----------------------

1. Broker Truth Reconciliation
Use:
- Makes Alpaca the source of truth.
- Compares dashboard/agent positions with real broker positions.
- Helps detect if the dashboard thinks a trade is open but Alpaca is flat.

Added/updated files:
- backend\broker_reconciler.py
- backend\ktrade_alpaca.py

Before v10.3:
- Project had simpler reconciliation.
- It could show positions/orders, but closed-trade P&L and broker fill truth were not as strong.

After v10.3:
- Real Alpaca fill history is used to reconstruct closed trades.
- Backend exposes broker truth endpoints.

Important endpoints:
- /reconciliation
- /reconcile_truth
- /closed_trades


2. Verified Closed Trades
Use:
- Builds closed trades from real Alpaca fill activities.
- Calculates P&L only from real broker fills.
- Helps avoid fake or estimated trade results.

Added/updated files:
- backend\broker_reconciler.py
- backend\ktrade_alpaca.py

Before v10.3:
- Dashboard could show positions and orders.
- Closed trade reporting was weaker or based more on dashboard/order state.

After v10.3:
- Closed trades are reconstructed from Alpaca fill activity.
- Win rate, profit factor, average win/loss can be based on actual fills.


3. Bad Price / Decimal Shift Guard
Use:
- Protects the strategy from wrong market prices.
- Example: if real price is around 236 but bad data says 2300, this guard blocks it.
- Prevents false momentum signals caused by bad ticks.

New files:
- data\price_sanity.py
- data\test_price_sanity.py

Updated files:
- data\ktrade_data.py
- agent\ktrade_agent_v9.py

Before v10.3:
- A bad market tick could enter the data pipeline.
- Strategy might treat a bad spike as strong momentum.

After v10.3:
- Bad ticks are rejected or carried forward using last-good price.
- RiskEngine also checks price again before approving a trade.


4. Safer Paper Execution
Use:
- Adds a broker adapter for Alpaca paper bracket orders.
- The agent records a position only after broker confirms a fill.
- Prevents phantom positions.

New file:
- agent\broker_adapter.py

Updated file:
- agent\ktrade_agent_v9.py

Before v10.3:
- Execution could simulate or assume fills more easily.
- There was more risk of dashboard/agent state drifting from broker truth.

After v10.3:
- Broker fill confirmation is required before recording a real trade fill.
- If an order is not filled, the agent does not record it as a real position.


5. Risk Engine Improvements
Use:
- Tracks real open positions.
- Syncs positions from broker truth.
- Updates equity from broker account.
- Enforces max positions and exposure more correctly.

Updated file:
- agent\ktrade_agent_v9.py

Before v10.3:
- Some risk checks could depend on internal state.
- If internal state was stale, risk checks could be less reliable.

After v10.3:
- RiskEngine can sync from broker positions.
- Daily loss guard can use broker equity.
- Position limits are more reliable.


6. Strategy Confirmation Fix
Use:
- Score alone is not enough.
- The strategy that produced the score must also confirm a signal.

Updated file:
- agent\ktrade_agent_v9.py

Before v10.3:
- A ticker could score well but the executable strategy signal might not fully confirm.

After v10.3:
- If score says MOMENTUM, momentum signal must confirm.
- If score says MACD_EMA, MACD/EMA signal must confirm.
- ORB is used only for intraday data.


7. ORB Intraday Fix
Use:
- ORB means Opening Range Breakout.
- It only makes sense on intraday candles, not daily candles.

Updated file:
- agent\ktrade_agent_v9.py

Before v10.3:
- ORB could influence score where it should not.

After v10.3:
- ORB only contributes on intraday data.
- Daily scoring weights are adjusted without ORB.


8. VectorBT Look-Ahead Fix
Use:
- Prevents backtest from using same-bar future-like information.
- Signal created on bar T is acted on at bar T+1.

Updated file:
- ktrade_vectorbt.py

Before v10.3:
- Backtest could be slightly too optimistic if signal and trade happened on same bar.

After v10.3:
- Entries and exits are shifted by one bar.
- Backtest becomes more realistic.


9. Market Hours / Holiday Awareness
Use:
- Helps the agent understand market timing better.
- Includes weekend and 2026 holiday checks.

Updated file:
- agent\ktrade_agent_v9.py

Before v10.3:
- Market phase logic was simpler.

After v10.3:
- Uses America/New_York market timing more carefully.
- Avoids trading-day mistakes on weekends/holidays.


10. v10.3 Test Suite
Use:
- Confirms safety fixes are working.
- Checks bad-price guard, broker fill behavior, risk logic, and strategy logic.

New file:
- test_v103_fixes.py

Before v10.3:
- No dedicated v10.3 safety test suite.

After v10.3:
- Test suite proves important safety fixes.
- Expected result: 22 passed, 0 failed.


Important File List
===================

backend\ktrade_alpaca.py
------------------------
Use:
- Main backend server.
- Starts dashboard at http://localhost:5001
- Connects to Alpaca paper account.
- Provides account, positions, prices, orders, buy/sell APIs.
- Provides broker reconciliation endpoints.

Before version:
- Basic Alpaca backend and dashboard bridge.

v10.3 version:
- Adds broker truth endpoints.
- Adds verified closed trades endpoint.
- Adds client_order_id support for traceable orders.

Run command:
cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe backend\ktrade_alpaca.py


backend\broker_reconciler.py
----------------------------
Use:
- Reconstructs closed trades from real Alpaca fill activities.
- Detects broker/agent position desync.
- Calculates verified P&L stats.

Before version:
- This file did not exist.

v10.3 version:
- New broker-truth reconciliation engine.

Do you run it directly?
- No. Backend imports it automatically.


agent\ktrade_agent_v9.py
------------------------
Use:
- Main signal scanner and agent logic.
- Scores stocks.
- Applies strategy rules.
- Applies risk checks.
- Produces BUY/WATCH signals.

Before version:
- Scanner and agent existed.
- Had scoring, strategy, and risk logic.

v10.3 version:
- Adds better strategy confirmation.
- Adds bad-price hard gate.
- Adds real broker position/equity sync support.
- Adds broker-confirmed fill logic.
- Adds market-hours and holiday logic.
- ORB is intraday-only.

Run command:
cd /d C:\trading-agent\Ktrade_v10_3
set KTRADE_DATA_PROVIDER=yfinance
set KTRADE_SCAN_SYMBOLS=
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_SCAN_INTERVAL=1d
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only


agent\broker_adapter.py
-----------------------
Use:
- Connects agent execution logic to Alpaca paper backend.
- Submits paper bracket orders.
- Waits for broker fill confirmation.

Before version:
- This file did not exist.

v10.3 version:
- New adapter for safer paper execution.

Do you run it directly?
- No. Agent imports/uses it when wired.


data\ktrade_data.py
-------------------
Use:
- Market data feed layer.
- Gets prices/history from Polygon, Alpaca, or yfinance depending on settings.
- Feeds scanner and strategy logic.

Before version:
- Data source/fallback logic existed.

v10.3 version:
- Adds price sanity guard into snapshot flow.
- Bad prices can be filtered before scanner sees them.

Do you run it directly?
- No. Agent imports it automatically.


data\price_sanity.py
--------------------
Use:
- Detects bad prices, decimal-shift errors, non-positive prices, large suspicious jumps.
- Protects scanner and risk approval path.

Before version:
- This file did not exist.

v10.3 version:
- New bad-tick safety guard.

Do you run it directly?
- No. Used by data and agent files.


data\test_price_sanity.py
-------------------------
Use:
- Test file for price sanity guard.
- Confirms bad KLAC-style 10x price is blocked.

Before version:
- This file did not exist.

Run command:
cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe data\test_price_sanity.py


frontend\KTrade_preview.html
----------------------------
Use:
- Main dashboard UI.
- Shows signals, positions, trades, alerts, performance.
- Calls backend APIs.

Before version:
- Dashboard existed.

v10.3 version:
- Uses backend signals dynamically.
- Shows trade type/timeframe/hold/exit rule.
- Shows broker reconciliation status.

Do you run it directly?
- No. Backend serves it at http://localhost:5001


ktrade_vectorbt.py
-----------------
Use:
- Daily/swing backtest engine.
- Tests strategy historically.
- Creates approved parameters.

Before version:
- VectorBT backtest existed.

v10.3 version:
- Adds one-bar signal shift to avoid look-ahead bias.

Run command:
cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe ktrade_vectorbt.py


ktrade_intraday_vectorbt.py
--------------------------
Use:
- Intraday VectorBT backtest engine.
- Tests intraday strategies historically.

Before version:
- Already existed.

v10.3 version:
- No major change compared with your current copied version.

Run command:
cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe ktrade_intraday_vectorbt.py --fast --universe extended


risk\ktrade_risk.py
-------------------
Use:
- Risk rule definitions and risk guard helpers.

Before version:
- Already existed.

v10.3 version:
- Same or mostly unchanged compared with previous package.

Do you run it directly?
- No. Used by agent/risk logic.


test_v103_fixes.py
------------------
Use:
- v10.3 safety test suite.
- Confirms important fixes work.

Before version:
- This file did not exist.

Run command:
cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe test_v103_fixes.py

Expected result:
- 22 passed, 0 failed.


Run Script Files
================

START_BACKEND.cmd
-----------------
Use:
- Double-click shortcut to start backend.

Before version:
- Not in original zip. Added locally for your convenience.


RUN_SCANNER_DAILY.cmd
---------------------
Use:
- Double-click shortcut to run daily scanner.

Before version:
- Added locally for your convenience.


RUN_SCANNER_INTRADAY.cmd
------------------------
Use:
- Double-click shortcut to run intraday scanner.

Before version:
- Added locally for your convenience.


RUN_TESTS.cmd
-------------
Use:
- Double-click shortcut to run test_v103_fixes.py and data\test_price_sanity.py.

Before version:
- Added locally for your convenience.


RUN_COMMANDS_V10_3.txt
----------------------
Use:
- Command reference document.
- Explains daily commands.

Before version:
- Added locally for your convenience.


KTrade_v10_3_Daily_Run_Guide.txt / .md
--------------------------------------
Use:
- Daily user guide with commands and explanations.

Before version:
- Added locally for your convenience.


What You Normally Run Daily
===========================

CMD 1: Backend
--------------
cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe backend\ktrade_alpaca.py

CMD 2: Daily scanner
--------------------
cd /d C:\trading-agent\Ktrade_v10_3
set KTRADE_DATA_PROVIDER=yfinance
set KTRADE_SCAN_SYMBOLS=
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_SCAN_INTERVAL=1d
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only

Open dashboard:
http://localhost:5001


What You Run For Intraday
=========================

cd /d C:\trading-agent\Ktrade_v10_3
set KTRADE_DATA_PROVIDER=yfinance
set KTRADE_SCAN_SYMBOLS=
set KTRADE_SCAN_UNIVERSE=extended
set KTRADE_SCAN_INTERVAL=5m
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only


What You Do Not Need To Run Daily
=================================

Do not run daily unless needed:
- ktrade_vectorbt.py
- ktrade_intraday_vectorbt.py
- test_v103_fixes.py
- data\test_price_sanity.py

Use VectorBT only when:
- Strategy logic changed
- New stocks added
- You want fresh approved parameters
- You want fresh backtest report

Use tests only when:
- You updated files
- Something is not working
- Before Git push/deployment


Final Recommendation
====================
Use v10.3 as your current testing version.
Keep the older project as backup.
Do not overwrite old project until v10.3 is tested for a few days.

============================================================
LATEST UPDATE - FINNHUB INTEGRATION
============================================================

What was done now
-----------------
Finnhub was added to the KTrade v10.3 project as an optional market-data, quote, and news source.
This does not replace Alpaca paper trading. Alpaca is still the broker truth for account, positions, orders, and paper trades.
Finnhub is used to improve quote/news support and provide another fallback when Polygon or Alpaca data has issues.

Files updated now
-----------------
1. data\ktrade_data.py
   Use: Main scanner/backtest market-data layer.
   What changed: Added Finnhub API support for historical candles and quote snapshots.
   Before: Data flow was mainly Polygon -> Alpaca -> yfinance.
   Now: Data flow can be Polygon -> Alpaca -> Finnhub -> yfinance when provider is auto.
   Direct mode: set KTRADE_DATA_PROVIDER=finnhub.

2. backend\ktrade_alpaca.py
   Use: Backend server for dashboard, Alpaca paper trading, positions, orders, prices, and API routes.
   What changed: Added Finnhub helper, quote fallback, and a news API endpoint.
   New endpoint: http://localhost:5001/news/AAPL
   Existing endpoint improved: http://localhost:5001/quote/AAPL

3. .env.template
   Use: Example environment file.
   What changed: Added FINNHUB_API_KEY example variable.

4. .env
   Use: Your local secret settings.
   What changed: Added FINNHUB_API_KEY placeholder if it was missing.
   You must paste your real Finnhub key there.

5. RUN_SCANNER_FINNHUB_DAILY.cmd
   Use: Runs the daily scanner using Finnhub as the data provider.

6. RUN_SCANNER_FINNHUB_INTRADAY.cmd
   Use: Runs the intraday scanner using Finnhub as the data provider.

7. KTrade_v10_3_Finnhub_Integration_Guide.txt
   Use: Separate short guide explaining Finnhub setup and commands.

How to configure Finnhub
------------------------
Open this file:
C:\trading-agent\Ktrade_v10_3\.env

Add or update this line:
FINNHUB_API_KEY=your_real_finnhub_key_here

How to run with Finnhub daily scanner
-------------------------------------
Open CMD 1 for backend:
cd /d C:\trading-agent\Ktrade_v10_3
.venv\Scripts\python.exe backend\ktrade_alpaca.py

Open CMD 2 for Finnhub daily scanner:
cd /d C:\trading-agent\Ktrade_v10_3
RUN_SCANNER_FINNHUB_DAILY.cmd

Open dashboard:
http://localhost:5001

How to run with Finnhub intraday scanner
----------------------------------------
Open CMD 2 and run:
cd /d C:\trading-agent\Ktrade_v10_3
RUN_SCANNER_FINNHUB_INTRADAY.cmd

Important explanation
---------------------
Alpaca = paper trading, broker account, positions, orders, source of truth.
Finnhub = quotes, news, optional candles, fallback market data.
Polygon = primary market data if working and plan allows it.
yfinance = final backup/fallback for testing and history.

So after this update, Finnhub helps the project get more data/news, but it does not place trades. Trades still go through Alpaca paper trading only.

