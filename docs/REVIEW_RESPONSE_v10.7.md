# KTrade v10.7 — Response to the v10.6 production-readiness review

All 12 issues from the review are addressed. Built on your v10.6 (sector cap and
everything else preserved). Test status:

```
test_v103_fixes.py        22/22
test_v105_sector_cap.py    9/9
test_v106_fixes.py         6/6
test_v107_fixes.py        18/18   (new, covers the items below)
data/test_price_sanity.py  pass
py_compile (all .py)       pass
```

Legend: ✅ done & tested · ◑ done, backend-only (compile-verified, needs live broker to exercise)

| # | Issue | Status |
|---|-------|--------|
| 1 | Dashboard `/buy` `/sell` bypass RiskEngine | ◑ |
| 2 | Kill switch logs but doesn't flatten/cancel | ✅ |
| 3 | State only in memory (lost on restart) | ✅ |
| 4 | No daily reset in autonomous loop | ✅ |
| 5 | DataFrame schema not validated | ✅ |
| 6 | Scan JSON not schema-validated | ✅ |
| 7 | Walk-forward look-ahead / leakage | ◑ |
| 8 | Live scorer uses incomplete current bar | ✅ |
| 9 | Partial fills not handled | ✅ |
| 10 | Backend state race conditions | ◑ |
| 11 | `await_fill` scans latest-20 instead of order id | ◑ |
| 12 | `PriceStreamer` recursive reconnect | ◑ |

---

### 1. ◑ Dashboard orders now go through RiskEngine + schema validation
New `backend/manual_order_schema.py` (`ManualOrderRequest`) strictly validates
ticker/side/qty/limit. New `_risk_gate_manual()` in the backend builds a
`RiskEngine`, syncs broker equity + positions, seeds the price reference, and
calls `RiskEngine.evaluate()` — so manual `/buy` `/sell` now hit the **same**
sector cap, max-positions, daily-loss, duplicate/cooldown, and price-sanity
checks as the agent. Approved BUYs are also risk-sized (capped at the engine's
`approved_qty`). Still gated by `KTRADE_PAPER_ORDER_SUBMISSION` + admin token.
Schema validation is unit-tested; the full route needs a live broker to exercise.

### 2. ✅ Kill switch flattens / cancels and persists
New `risk/emergency.py` (`EmergencyController`): on trigger it cancels all open
broker orders, optionally flattens positions, **persists the kill reason to
`data/kill_switch.json`**, and survives restart (the CEO restores it on startup
and refuses to trade while active). `RiskEngine.activate_kill_switch()` and
`_close_all()` now call it. New backend `cancel_all_orders()` /
`close_all_positions()`; the broker adapter exposes both. Tested: cancel+flatten
fire, state persists across a restart, and reset clears it.

### 3. ✅ Risk state persisted across restarts
New `risk/state_store.py` (atomic writes). `RiskEngine` gained
`serialize_state()` / `restore_state()` / `persist_state()`, persisting
cooldowns, duplicate windows, daily ticker counts, counters, equity baseline,
and price refs after every fill/kill. The CEO restores it on startup. Tested
round-trip.

### 4. ✅ Daily reset in the loop
`KTradeCEO._roll_trading_day_if_needed(equity)` runs every cycle: on a new
calendar day it calls `reset_day()`, resets `equity_open`, and resets the loss
guard. Tested with a forced stale day.

### 5. ✅ Strict OHLCV validation
New `data/schema_validation.py::normalize_ohlcv_frame()` lowercases columns,
coerces strings to numbers, drops NaN/inf, de-dups and sorts the index, and
rejects impossible OHLC rows. `rank_universe()` now normalizes every frame and
skips bad ones with a warning. Tested (uppercase + string inputs; raises on
missing columns).

### 6. ✅ Scan JSON validated before dashboard/AI use
New `data/scan_schema.py` (`ScanPayload`, `load_valid_scan_payload`). `/all`
validates `ktrade_scan_latest.json`; invalid JSON returns a safe empty payload
with an error instead of leaking into the AI advisor. Tested (valid passes,
negative price → safe empty).

### 7. ◑ Walk-forward signal shift
`WalkForwardValidator.validate()` now shifts entries/exits one bar before
`from_signals()` (matching `_run_portfolio`), removing same-bar look-ahead. The
deeper **true train/test walk-forward** (optimize only on train split, test on
the next) is still the recommended follow-up — it's a larger optimization-loop
rewrite. Compile-verified (vectorbt not installed in CI).

### 8. ✅ Drop the unclosed current bar
`data/schema_validation.py::drop_unclosed_last_bar()` removes a still-forming
last candle; `rank_universe()` calls it before scoring. Tested.

### 9. ✅ Partial fills
New `risk/position_fills.py::apply_fill_to_position()`: a SELL reduces quantity
(weighted-avg cost preserved) instead of popping the whole long; a BUY blends
average cost. `RiskEngine.record_fill()` now uses it. The broker adapter's
`await_fill()` reports `partially_filled` explicitly. Tested (blend, partial
sell, full sell).

### 10. ◑ Backend state lock
Added `STATE_LOCK` (RLock) with `update_state()` / `snapshot_state()`. The main
refresh-loop write is now atomic, `/all` reads a locked snapshot, and live-price
updates take the lock — closing the "refresh overwrites during JSON response"
race. Remaining ad-hoc mutations should migrate to `update_state()` over time.
Compile-verified.

### 11. ◑ `await_fill` fetches the specific order
New backend `fetch_order(order_id)`. The adapter's `await_fill()` now polls that
specific order (falling back to client-order-id match), handles
`partially_filled`, and treats `canceled/expired/rejected` as terminal — no more
`not_found` from the latest-20 list. Compile-verified.

### 12. ◑ `PriceStreamer` reconnect loop
Reconnect is now a `while self.running:` loop instead of recursive `self._run()`
— no stack growth on repeated disconnects. Compile-verified.

---

## Dependency
Added `pydantic>=2.7.0` to `requirements.txt` (used by the schema modules; they
degrade gracefully if it's missing, but install it).

## Still open by design (matches the review's pre-trade checklist)
- True train/test walk-forward optimization (#7 deeper fix).
- API retry/backoff on transient broker errors.
- A real **1-share Alpaca paper bracket round-trip** — the final gate.
- Keep `KTRADE_PAPER_ORDER_SUBMISSION=false` and `LIVE_TRADING=false` until that
  round-trip is verified.

## Verify
```cmd
cd ktrade_v10.7
pip install -r requirements.txt
python test_v103_fixes.py
python test_v105_sector_cap.py
python test_v106_fixes.py
python test_v107_fixes.py
python data\test_price_sanity.py
```
