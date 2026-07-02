# KTrade v10.3 — Developer Feedback Review & Response

This document answers the agent-logic review point by point. Each item shows the
**status**, what the code now does, where it changed, and how it's verified.
Every "Fixed" item below is covered by an automated check in
`test_v103_fixes.py` (**22/22 passing**).

Status legend: ✅ Fixed · ◑ Partially addressed (honest scope) · ⏳ Deferred (with reason)

---

## Summary table

| # | Reviewer item | Status |
|---|---------------|--------|
| 1 | Momentum scored but not executable (scoring vs trading disagree) | ✅ Fixed |
| 2 | Price-sanity guard not a final trade gate | ✅ Fixed |
| 3 | Risk engine doesn't track real open positions / equity | ✅ Fixed |
| 4 | `record_fill()` called before a real broker fill | ✅ Fixed |
| 5 | `ExecutionAgent` doesn't actually execute | ✅ Fixed (paper, opt-in) |
| 6 | ORB runs on daily bars | ✅ Fixed |
| — | Daily loss guard uses static equity | ✅ Fixed |
| — | Position sizing done twice / unclear | ✅ Fixed (sole sizer) |
| — | Cost optimizer counts 1 call for a whole scan | ✅ Fixed |
| — | Market-hours logic too simple (no TZ/holidays) | ✅ Fixed |
| 9 | Static market state (VIX/SPY) | ◑ Partial |
| 8 | Approved VectorBT params don't drive live signals | ◑ Partial |
| 7 | Backtest walk-forward leakage | ◑ Partial (look-ahead shift in; split rebuild deferred) |
| 10 | Duplicate/overlapping files | ⏳ Deferred (documented) |

The reviewer's stop-ship gate was "do not run autonomously until items 1–5 are
fixed." **Items 1–5 are fixed**, plus the sizing, equity, cost, and market-hours
body items.

---

## 1. ✅ Momentum scored but not executable

**Was:** `ConvictionScorer` labelled most high-scoring names `"MOMENTUM"`, but
`run_cycle` only executed if `macd` or `orb` fired, and `StrategyAgent` only
emitted `macd`/`orb`. Pure-momentum names scored high and then placed zero trades.

**Now:**
- `StrategyAgent.run()` emits executable `momentum` and `trend` signals (plus
  `macd`, and `orb` only on intraday data).
- `run_cycle` no longer accepts "any signal." It maps the **scored** strategy
  label to its signal key and requires *that* signal to confirm:
  `MACD_EMA→macd`, `MOMENTUM→momentum`, `ORB→orb`. A name scored as MOMENTUM
  now needs the momentum rule to actually fire, and a blocked reason is logged
  if it doesn't.

**Verify:** `F1 StrategyAgent emits momentum/trend keys`, `F1 momentum fires on
clean uptrend`.

---

## 2. ✅ Price-sanity guard is now a final trade gate

**Was:** `price_sanity.py` was only applied at snapshot ingestion. The exact
KLAC `$2300.52` decimal-shift tick still passed `RiskEngine.evaluate()`.

**Now:** `RiskEngine.evaluate()` calls `PRICE_GUARD.validate_entry(ticker,
price, reference)` as check **1b — before any sizing**. A bad tick returns
`BAD PRICE: <reason>` and never reaches sizing. `RiskEngine.seed_references()`
seeds prior closes each morning (and feeds `PRICE_GUARD`), so the first live
tick is validated too. `sync_positions()` and `record_fill()` also refresh
per-ticker references from broker truth.

**Verify:** `F2 bad KLAC tick BLOCKED at RiskEngine` (px=2300.52, ref=236 →
blocked) and `F2 good KLAC price approved` (px=238 → approved).

---

## 3. ✅ Risk engine tracks real open positions and equity

**Was:** `open_positions` was never populated, so `max_open_positions`,
duplicate-long, and exposure checks were dead. Equity was static.

**Now:**
- `RiskEngine.record_fill()` records the open long (qty + avg cost) on buys and
  clears it on sells, so position-count and exposure checks are live.
- New `RiskEngine.sync_positions(broker_positions)` replaces in-memory state
  with broker truth (shape from `fetch_positions()`), and `update_equity()`
  takes the real account equity.
- `run_cycle` pulls **broker truth first** when a broker adapter is attached:
  `get_account()` → `update_equity`, `get_positions()` → `sync_positions`. If
  that sync fails, the cycle halts rather than trading on stale state.

**Verify:** `F3 open_positions tracked after buys`, `F3 MAX POSITIONS now
enforced`, `F3 sync_positions replaces with broker truth`.

---

## 4. ✅ `record_fill()` only after a confirmed broker fill

**Was:** `ExecutionAgent.execute()` logged a placeholder, added a tracked
position, and `run_cycle` immediately called `record_fill()` — creating fake
state with no broker order.

**Now:** `ExecutionAgent.execute()` returns a structured result
`{filled, qty, price, simulated, reason}`. `run_cycle` calls `record_fill()`
**only when `filled` is True**. With no broker attached, `execute()` returns
`filled=False, simulated=True` — it never claims a fill. Approved-but-unfilled
orders are logged as blocked with the broker status.

**Verify:** `F4 no-broker execute does NOT report a fill`, `F4 unfilled order →
no fill recorded`.

---

## 5. ✅ Real Alpaca bracket execution (paper, opt-in)

**Was:** execution was a logging shell.

**Now:** new `agent/broker_adapter.py` (`AlpacaBrokerAdapter`) implements the
execution contract against the existing backend:
- `submit_bracket(...)` posts a real **bracket** order (market entry + stop-loss
  + take-profit) to the **paper** endpoint, with a deterministic
  `client_order_id`.
- It is a hard **no-op unless `KTRADE_PAPER_ORDER_SUBMISSION=true`** — order
  submission stays off by default.
- `await_fill()` polls real broker orders by `client_order_id` and only returns
  a fill when the broker reports `filled`. It never fabricates one.

Wire it with `KTradeCEO(broker=AlpacaBrokerAdapter())`.

**Verify:** `F5 confirmed broker fill recorded` (fake broker confirms → fill
recorded with real qty/price).

---

## 6. ✅ ORB only on intraday data

**Was:** the daily scan built an "opening range" from daily bars, so ORB was
mostly noise/neutral.

**Now:** `ConvictionScorer.score(ticker, df, interval)` is timeframe-aware. On
daily bars the ORB component is **not computed** and is dropped from the weights
(`WEIGHTS_DAILY` renormalised; `WEIGHTS_INTRADAY` keeps ORB). The strategy label
can only be `"ORB"` on intraday data. `StrategyAgent.run(..., intraday=...)`
only evaluates ORB intraday.

**Verify:** `F6 daily score has no orb component`, `F6 daily strategy label is
never ORB`, `F6 intraday score includes orb component`.

---

## Daily loss guard — ✅ real equity

`run_cycle` previously used `equity = CFG.account_value`. It now uses the real
broker equity when an adapter is attached (falling back to config only when
running dry). The loss guard checks against that real number.

---

## Position sizing — ✅ RiskEngine is the sole sizer

**Was:** the CEO pre-computed a share count, then `_size()` resized again —
hard to reason about, and not true risk-dollar sizing.

**Now:** `TradeRequest` carries `desired_risk_dollars`. The CEO sends a risk
budget (and `qty=0`); `RiskEngine._size()` is the **only** place a share count
is produced — risk-dollar sizing capped by `max_trade_dollar_risk` and clamped
by Kelly. The dollar-at-risk is bounded by the per-trade cap.

**Verify:** `Sizer: RiskEngine sizes from budget (qty=0 in → qty>0 out)`,
`Sizer: dollar risk within max_trade_dollar_risk`.

---

## Cost optimizer — ✅ counts real call volume

**Was:** `record_call(0.01 * len(data_map))` incremented the counter by 1.

**Now:** new `record_calls(count, cost)` increments by the real count; `run_cycle`
passes `count=len(data_map)`. Day-rollover logic preserved.

**Verify:** `F12 record_calls counts 100, not 1`.

---

## Market-hours — ✅ timezone + weekends + holidays

**Was:** `datetime.now()` with no timezone; could think the market is open on a
weekend depending on machine TZ.

**Now:** `HeartbeatEngine` resolves time in `America/New_York` (`zoneinfo`),
rejects weekends, and checks a 2026 NYSE holiday set. `is_market_open()` and
`get_phase()` both respect it. (Half-day early closes are a known follow-up.)

**Verify:** `F10 weekend not a trading day`, `F10 holiday not a trading day`,
`F10 normal weekday is a trading day`.

---

## 9. ◑ Live market state — partially addressed

**Done:** new `KTradeCEO._refresh_market_state()` runs at the top of every
cycle. It (a) pulls live values from an injected `market_fn` if provided, and
(b) derives an intraday **flash-crash** flag from the SPY bar in `data_map`
(≤ `-flash_crash_drop_pct` vs the prior bar). If no live feed is wired it logs
that the state is **STALE** instead of silently trusting `VIX=18.0`.

**Still open / honest gap:** there is no real VIX feed bundled. `market_fn`
(see `broker_adapter.make_market_fn()`) is the injection point — wire a real
`^VIX` source there. Until then, VIX-based circuit breakers remain advisory.

**Verify:** `F9 SPY -5% bar trips flash-crash flag`.

---

## 8. ◑ Approved VectorBT params driving live signals — partial

The plumbing is present (`load_approved_params`, `get_ticker_params`) and the
scorer is now timeframe-aware, but the individual strategy functions still use
their built-in parameters rather than per-ticker approved params. **I did not
claim this as fixed.** Fully wiring it means parameterising each strategy's
signal function to accept and use the approved params — a contained but real
piece of work that belongs in its own pass so it can be tested per strategy.

**Recommendation:** either complete the per-strategy parameterisation, or hide
"approved params" from the UI until they truly drive signals, so the dashboard
doesn't overstate what's live.

---

## 7. ◑ Backtest walk-forward leakage — partial

**Done:** `ktrade_vectorbt.py::_run_portfolio()` now shifts entries/exits by one
bar (`entries.shift(1)`, `exits.shift(1)`) before `Portfolio.from_signals`, so a
signal computed from bar *t* is acted on at *t+1*. That removes the same-bar
look-ahead that inflated returns across every strategy centrally.

**Still open / honest gap:** the bigger structural issue — optimising on the
full dataset and then "validating" on the same data — is **not** fixed. True
walk-forward needs: optimise on a train window → test on the next unseen window
→ expand → repeat, recording only out-of-sample metrics. That's a deliberate
rebuild of the optimisation loop and is deferred so it can be done and tested
properly rather than half-done.

---

## 10. ⏳ Duplicate/overlapping files — deferred (documented)

`ktrade_merged.py`, `trading_agent_master_backup.py`, and the
`PROJECT_DOCUMENTATION_PACKAGE/required_project_files/...` copies still exist. I
**did not delete** them in this pass — some run scripts and the bridge may import
them, and silently removing files is riskier than leaving them.

**Recommended single production path:**
```
agent/ktrade_agent_v9.py     # the live agent (this file)
agent/broker_adapter.py      # broker contract  (NEW v10.3)
risk/ktrade_risk.py          # standalone risk module
data/ktrade_data.py          # market data feed
data/price_sanity.py         # bad-tick gate
backend/ktrade_alpaca.py     # Flask + Alpaca REST
```
Archiving `ktrade_merged.py` and `*_backup.py` into an `archive/` folder is the
suggested next step once you confirm nothing imports them.

---

## What changed on disk (v10.3)

- `agent/ktrade_agent_v9.py` — items 1–6, sizing, equity, cost, market-hours,
  market-state hook, broker sync.
- `agent/broker_adapter.py` — **NEW**: paper bracket execution + truth sync +
  fill confirmation.
- `ktrade_vectorbt.py` — one-bar signal shift (look-ahead fix).
- `test_v103_fixes.py` — **NEW**: 22 checks covering every "Fixed" item above.

## Reviewer's autonomy gate

> "do not run this agent autonomously yet, even in paper, until at least items
> 1–5 are fixed."

Items 1–5 are fixed and tested. Before live paper autonomy, the remaining
sensible gates are: set `KTRADE_PAPER_ORDER_SUBMISSION=true` only after a dry
run, wire a real VIX feed into `market_fn`, and validate one real Alpaca paper
round-trip end-to-end (still on the deferred list).
