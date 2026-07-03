# KTrade — Buy/Sell Rule Audit (against docs/RISK_RULES.md)

Audited files: `agent/ktrade_agent_v9.py`, `risk/ktrade_risk.py`,
`data/ktrade_signals.py`, `data/ktrade_approved_params.json` (33 entries) +
`ktrade_intraday_approved_params.json` (119), `docs/RISK_RULES.md`,
`ktrade_agent.log`, `data/ktrade_backtest_latest.json`, and the uploaded
`traderadar-trades-2026-06-23.csv` (control).

---

## 1. Active buy/sell rules (as actually implemented)

### BUY — an entry requires ALL of:
1. **Conviction ≥ floor.** `ConvictionScorer` (momentum, volume, trend, MACD,
   ORB, relative-strength). Floor = `min_conviction_score` — now **75** (see
   Discrepancy #2).
2. **The scored strategy's own signal confirms** that cycle (MACD / momentum /
   trend / ORB-intraday). Per-ticker parameters come from the approved-params
   JSON.
3. **RiskEngine.evaluate() passes every gate:** kill switch off → bad-tick price
   gate (+ no-reference refusal) → flash-crash → VIX → daily loss/drawdown →
   max positions → **sector cap (30% / 3 per sector)** → duplicate window (60s) →
   per-day count (3) → cooldown (300s) → buying-power.
4. **Sizing is risk-dollar:** stop = entry − ATR×1.5 (or 2% fallback); qty sized
   so dollar-risk ≤ $1,000, capped by 10% position size and ¼-Kelly.

### SELL / exit — whichever fires first:
- **Stop** = entry − ATR×1.5 (broker-side bracket leg).
- **Target** = +4% / 2:1 R:R (broker-side bracket leg).
- **Trailing stop** ratchets up as price rises (`PositionMonitor`).
- Bracket legs sit on Alpaca, so exits fire even if the agent is offline.

### Scope
Autonomous loop is **long-only** — it never opens shorts and never trades
options, even though `ALLOW_SHORTS=True` and `MAX_SHORT_EXPOSURE_PCT=15%` exist.

---

## 2. Discrepancies (intended vs implemented)

| # | Severity | Finding |
|---|----------|---------|
| 1 | **Critical (process)** | `RISK_RULES.md` said *“edit `risk/ktrade_risk.py` → RiskConfig.”* The live agent **does not import that module** — it uses `class CFG` in `agent/ktrade_agent_v9.py`. Editing `RiskConfig` changes nothing. **Fixed in v10.8:** doc now points to `CFG`/`.env`. |
| 2 | **Major** | `min_conviction_score` defaulted to **60** in code vs **75** documented. It only became 75 if `.env` was present — and `.env` isn't shipped (template only), so a fresh unzip ran at 60. **Fixed in v10.8:** code default is now 75. |
| 3 | **Major (dormant)** | VIX breakers (25/28/30/50) are coded and documented but **inert** — there is no live VIX feed, so `MARKET.vix` stays 18.0 (the log confirms *“Market state STALE … VIX=18.0 assumed”*). None of the VIX rules fire live. **Open:** wire a real VIX source into `market_fn`. |
| 4 | **Major (evidence)** | `data/ktrade_backtest_latest.json` is **stale v10.0** (run 2026-06-18, Windows path), so it is **not** a valid control for v10.7/8 behavior. Regenerate before using it for backtest-vs-live alignment. |
| 5 | Minor | Two position caps coexist: `max_positions` (per-cycle = 3) and `max_open_positions` (total = 10). Only the latter is in `RISK_RULES.md`. Document the per-cycle cap. |
| 6 | Minor | `ktrade_agent.log` is **test-harness output**, not live/paper trades — it cannot be used to detect live rule drift. It does, however, confirm the rules *fire* (duplicate block, kill switch, scan-JSON rejection all logged). |

Everything else in `CFG` matches `RISK_RULES.md` exactly: drawdown 3%, loss
$3,000, 10 positions, 10% / $1,000 per trade, dup 60s / cooldown 300s / 3-per-day,
flash-crash 2.5%, ATR 1.5, stop 2% / target 4%, Kelly 0.55 / 1.8 / 0.25.

---

## 3. Control-group analysis — TradeRadar CSV vs KTrade rules

The uploaded `traderadar-trades-2026-06-23.csv` is the **peer** system (not
KTrade). Run against KTrade's documented rules (account = $100k):

| KTrade rule | TradeRadar violations (of 290 trades) |
|---|---|
| Duplicate window (<60s same ticker+side) | **196** |
| Cooldown (<300s after same-ticker fill) | **211** |
| > 3 entries per ticker per day | **113** |
| Position > 10% of account | 3 (largest: **PL 748sh × $49.71 = $37,183 = 37%**) |

TradeRadar outcome over that window: 51% win rate, **net −$2,316**. KTrade's
duplicate/cooldown/per-day/size rules would have blocked the majority of that
churn — strong evidence the rule set targets real failure modes. (This validates
the rules; it is not a backtest of KTrade itself.)

---

## 4. Critical gaps / vulnerabilities

1. **Config/doc drift (was #1)** — now fixed, but worth a standing check: there
   is still a *second* config surface (`risk/ktrade_risk.py`) that can diverge
   from `CFG`. Recommend deleting or explicitly importing it so there's one
   source of truth.
2. **VIX breakers are decorative without a live feed (#3)** — on a real
   high-VIX day, none of the 25/28/30/50 rules trigger. This is the single most
   important *unfixed* gap before trusting the system through volatility.
3. **No fresh backtest control (#4)** — you can't currently detect live-vs-
   backtest drift because the backtest artifact is two versions old.

## 5. Proposed patches
- ✅ Done in v10.8: conviction default 75; `RISK_RULES.md` points to live `CFG`.
- ⏳ Wire `market_fn` to a real `^VIX` source (Alpaca/Polygon/yfinance) so the
  VIX breakers are live; until then treat them as inactive.
- ⏳ Regenerate `ktrade_backtest_latest.json` on v10.8 and keep it as the control.
- ⏳ Collapse `risk/ktrade_risk.py` into `CFG` (or delete) to remove the second
  config surface.
- ⏳ Document the per-cycle `max_positions=3` cap in `RISK_RULES.md`.
