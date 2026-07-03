# KTrade v10.6 — Response to the v10.5 code review

Each numbered issue from the review is mapped to what changed. Built on **your**
v10.5 (sector cap preserved). All four test suites pass:
`test_v103_fixes.py` 22/22 · `test_v105_sector_cap.py` 9/9 ·
`test_v106_fixes.py` 6/6 · `data/test_price_sanity.py` OK.

Status: ✅ fixed · ◑ partially fixed · 📝 manual (needs your environment)

| # | Review item | Status |
|---|-------------|--------|
| 1 | `--once` runs empty `{}` | ✅ |
| 2 | Price-sanity passes unseeded tickers | ✅ |
| 3 | Real equity baseline wrong (false daily-loss halt) | ✅ |
| 4 | Dashboard `/buy` `/sell` bypass RiskEngine | ◑ |
| 5 | Backend security (CORS + `0.0.0.0`) | ✅ |
| 6 | `run-paper-trade.ps1` broken | ✅ |
| 7 | `.cmd` files point to old folders | ✅ |
| 8 | Requirements incomplete | ✅ |
| 9 | `ask()` missing `anthropic-version` + timeout | ✅ |
| 10 | Stale scan/backtest outputs | 📝 |

---

### 1. ✅ `--once` now runs a real cycle
`main()`'s `--once` did `ceo.run_cycle({}, {})`. Now it builds the universe via
`PolygonDataFeed.batch_get(...)`, computes prices, **seeds prior-close
references**, runs one real cycle, and prints a trades/blocked/exits summary.

### 2. ✅ Price-sanity no longer passes unseeded tickers
Two layers:
- `run_cycle` now calls `_seed_price_references_from_data(data_map)` every cycle,
  so every scanned ticker has a prior-close reference before any risk approval.
- `RiskEngine.evaluate()` now **refuses a first BUY with no reference**
  (`NO PRICE REFERENCE …`) when `KTRADE_REQUIRE_PRICE_REF=true` (default). Your
  `NEWX @ 2300.52, reference=None` case is now blocked.
- Verified: `test_v106_fixes.py` — no-ref BUY blocked; approved once seeded; a
  bad decimal-shift still blocked even with a reference.

### 3. ✅ Real equity baseline (must-fix)
On the **first** broker sync, `run_cycle` sets `RiskEngine.equity_open` to the
real broker equity and calls `loss_guard.reset(equity)`, guarded by a new
`_equity_initialized` flag. The per-trade **risk budget now uses real `equity`**,
not `CFG.account_value`. Verified: broker equity $50k with `ACCOUNT_VALUE=100000`
no longer triggers a false "lost 50%" halt.

### 4. ◑ Dashboard `/buy` `/sell` — hardened, full wrap still pending
I did **not** fully route dashboard orders through the agent's `RiskEngine`
(that needs the backend to hold a live, position/equity-synced engine — too risky
to wire blind). Instead I added real guards to `/buy`, `/sell`, `/cancel`:
- **admin-token required** (`X-KTrade-Admin-Token`, see #5),
- **bad-tick price gate** on manual BUY via `PRICE_GUARD` against the live
  Alpaca snapshot,
- still gated by `KTRADE_PAPER_ORDER_SUBMISSION` (off by default).

**Recommendation stands:** keep `KTRADE_PAPER_ORDER_SUBMISSION=false` until these
endpoints run through the full RiskEngine (sector cap, max positions, cooldowns,
VIX/flash-crash). The new gates reduce risk but are not the full risk path.

### 5. ✅ Backend security
- `CORS(app)` → origins restricted to `KTRADE_ALLOWED_ORIGINS`
  (default `localhost`/`127.0.0.1`).
- `app.run(host="0.0.0.0")` → binds `KTRADE_BIND_HOST` (default `127.0.0.1`).
- New `require_admin()` + `KTRADE_ADMIN_TOKEN` enforced on `/buy`, `/sell`,
  `/cancel`. With no token configured, those endpoints return 403.

### 6. ✅ `run-paper-trade.ps1`
Rewritten as a **safe scanner-only** runner: removed the call to the missing
`ktrade_alpaca_trader.py`, fixed `KTRADE_DATA_SOURCE` → `KTRADE_DATA_PROVIDER`,
and added a note that real paper execution goes through the agent + broker
adapter (`--once`), not a separate trader script.

### 7. ✅ `.cmd` files
All `cd /d C:\trading-agent\Ktrade_v10_3|_5` lines replaced with portable
`cd /d "%~dp0"`, and `KTRADE_DATA_SOURCE` → `KTRADE_DATA_PROVIDER` everywhere.

### 8. ✅ Requirements
Added `vectorbt>=0.26.0` and `pytest>=8.0.0`. `transformers`/`torch` remain
commented as optional (heavy; only for local FinBERT).

### 9. ✅ `ask()` Anthropic header + timeout
Added `"anthropic-version": "2023-06-01"` and `timeout=30` to the agent's direct
Anthropic call.

### 10. 📝 Stale scan/backtest outputs
Can't regenerate here — needs your machine, a data key/network, and `vectorbt`
installed (now in requirements). After `pip install -r requirements.txt`, run:
```cmd
.venv\Scripts\python.exe ktrade_vectorbt.py --show
.venv\Scripts\python.exe ktrade_intraday_vectorbt.py --show
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only
```
then confirm `data/ktrade_scan_latest.json` carries the new `price_validation`,
`scan_interval`, and `trade_type` fields.

---

## Still open by design (unchanged stance)
- Full RiskEngine wrap of dashboard orders (#4).
- A real Alpaca paper round-trip with **1 share** after the above.
- Live VIX feed into `market_fn`; full walk-forward backtest rebuild.

**Bottom line matches the reviewer:** v10.6 is solid for score-only, dashboard
read-only, and unit testing. Keep `KTRADE_PAPER_ORDER_SUBMISSION=false` and
`LIVE_TRADING=false` until #4 and the paper round-trip are done.

## Verify
```cmd
cd ktrade_v10.6
python test_v103_fixes.py
python test_v105_sector_cap.py
python test_v106_fixes.py
python data\test_price_sanity.py
```
