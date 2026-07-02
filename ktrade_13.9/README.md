# KTrade PRO v13.9

> **Current version: v13.9** (package folder may be named per your deployment).
> Recent additions on top of the core agent (agent/ktrade_agent_v9.py):
> - v11.1 paper→live promotion gate · v11.2 regime strategy-switch
> - v11.3 portfolio source-of-truth feed + net-worth/staleness kill switches + hash-chained audit log
> - v12 manual-order hardening (trusted price reference, bracket routing, persistent risk engine) + autonomous-loop fix + release-safety check
> - v12.1 state-lock pass (atomic state writes/reads) + docs/version cleanup
>
> See the `V11.*_CHANGES.md` / `V12*` notes for details.
>
> **Safe quick start (always score-only first):**
> ```bash
> python agent/ktrade_agent_v9.py --score-only
> ```
> Do not start in autonomous mode until you have run score-only and paper validation.
### AI-Powered Trading Platform + Institutional Risk Engine
**iT LLC / HR1105114BCE Capital LLC**

---

## Core agent history (v8 → v9 lineage; package version is shown above)

| Fix | Description |
|-----|-------------|
| 🔴 **Polygon.io replaces yfinance** | Official institutional API, no rate limit surprises |
| 🔴 **Persistent state** | Positions survive server crashes (JSON → PostgreSQL ready) |
| 🟡 **CEO architecture** | 5 specialist agents, no monolithic blocking |
| 🟡 **Fibonacci signals** | ATH-break detection with Fib extension targets |
| 🟡 **MU Jun 24 setup** | $1089.29 ATH → $1699 (1.618★) primary target |
| 🟢 **Survivorship bias warnings** | Explicit disclaimers on all backtests |
| 🟢 **Developer feedback** | All 3 architecture flaws addressed |

---

## Full File Map

```
Ktrade_ChatGPT_V12.4/
│
├── README.md                          ← This file
├── requirements.txt                   ← pip install -r requirements.txt
├── .env.template                      ← Credentials template
│
├── agent/
│   └── ktrade_agent_v9.py             ← UNIFIED MASTER AGENT
│                                         Combines ALL modules:
│                                         v8.1 + v8.3 + MACD/EMA + ORB
│                                         + Heartbeat + CEO orchestrator
│                                         + KTrade risk engine
│                                         + Crash detection
│
├── data/
│   └── ktrade_data.py                 ← NEW: Polygon.io data feed
│                                         + Fibonacci extension analyzer
│                                         + Persistent state store
│                                         + VIX + Put/Call ratio
│
├── risk/
│   └── ktrade_risk.py                 ← Risk engine (3 bug fixes)
│                                         Kill switch, MDD, VIX breakers
│                                         Duplicate prevention, cooldown
│                                         Kelly sizing, bracket orders
│
├── backend/
│   ├── ktrade_alpaca.py               ← Alpaca paper/live bridge
│   └── ktrade_bridge.py               ← Robinhood fallback
│
├── frontend/
│   ├── KTrade_live.jsx                ← Live React dashboard
│   ├── KTrade_platform.jsx            ← Platform version
│   └── KTrade_preview.html            ← Open in browser (instant)
│
└── docs/
    ├── DEVELOPER_FEEDBACK_RESPONSE.md ← ⭐ Every dev feedback point addressed
    ├── ACTION_PLAN.md                 ← Full roadmap Phase 1-3
    ├── RISK_RULES.md                  ← Risk config guide
    ├── SETUP_GUIDE.md                 ← Step-by-step setup
    └── BROKER_CONNECTION.md           ← Alpaca + Robinhood guide
```

---

## Quick Start

```bash
# 1. Install all dependencies
pip install -r requirements.txt

# 2. Set credentials (.env or export)
export ALPACA_KEY="PKxxxxxxxxxxxxxxxx"
export ALPACA_SECRET="xxxxxxxxxxxxxxxx"
export POLYGON_KEY="xxxxxxxxxxxxxxxx"   # free at polygon.io

# 3. Test data connection
python data/ktrade_data.py

# 4. Run risk engine demo
python risk/ktrade_risk.py

# 5. Start agent (safe score-only mode first)
python agent/ktrade_agent_v9.py --score-only

# 6. Check MU Fibonacci setup
python agent/ktrade_agent_v9.py --ask "MU ATH break setup?"

# 7. Run crash detection
python agent/ktrade_agent_v9.py --crash-check

# 8. Full autonomous paper trading
python agent/ktrade_agent_v9.py
```

---

## Windows Dashboard Start

Open CMD window 1:

```cmd
cd /d C:\trading-agent\ktrade
powershell -ExecutionPolicy Bypass -File start-paper-bridge.ps1
```

Keep that window open, then visit:

```text
http://127.0.0.1:5001/
```

Open CMD window 2 whenever you want a fresh strategy scan:

```cmd
cd /d C:\trading-agent\ktrade
powershell -ExecutionPolicy Bypass -File run-score-only.ps1
```

For the extended universe scan:

```cmd
cd /d C:\trading-agent\ktrade
powershell -ExecutionPolicy Bypass -File run-score-extended.ps1
```

The extended scan includes:

```text
CEG, VST, GEV, ETN, PWR, VRT, MOD, MPWR, NVTS, TLN,
SOXX, XAR, IDGT, QTUM, DRAM, MRVL, MU, RMBS, LEU,
RGTI, QBTS, IONQ, TQQQ, NVAX, AMD, INTC, CRDO, PL,
NOK, ARM, NBIS, QCOM, MSTR, SMCI, IREN, CRWV, RKLB,
IRDM, KTOS, DXYZ, BTC-USD, ETH-USD
```

To scan only your own list:

```cmd
cd /d C:\trading-agent\ktrade
set KTRADE_SCAN_SYMBOLS=NVDA,AAPL,MSFT,QQQ
.venv\Scripts\python.exe agent\ktrade_agent_v9.py --score-only
```

To use the merged file with the same extended universe:

```cmd
cd /d C:\trading-agent\ktrade
set KTRADE_SCAN_UNIVERSE=extended
.venv\Scripts\python.exe ktrade_merged.py --score-only
```

The dashboard's **Sample Trade** screen uses a separate browser-only
$100,000 simulation wallet. It does not submit an Alpaca order.

---

## MU Jun 24 Trade Setup (from screenshot)

```
ATH: $1089.29
Condition: Price must CLEAR and HOLD above ATH

Extension Targets:
  $1322 (1.236) — first take-profit (use for partial exit)
  $1466 (1.382) — momentum continuation
  $1582 (1.500) — Wolfe zone resistance
  $1699 (1.618★) — PRIMARY TARGET (Daiwa $1,600 aligns)
  $2075 (2.000) — full extension / UBS target

Earnings binary: Jun 24
  Consensus EPS: ~$19.58
  Consensus Rev: ~$34.3B
  → Beat + ATH break = high conviction long
  → Miss = puts activated (crash detection agent flags this)
```

The agent monitors MU price vs $1089.29 and auto-triggers the
Fibonacci bracket order system when ATH clears.

---

## Architecture

```
You / Claude (Board)
       │
   [CEO Agent]          ktrade_agent_v9.py
       │
  ┌────┴────────────────────────────┐
  │         │         │         │   │
[Research][Strategy][Risk][Exec][Cost]
    │          │        │
[Polygon]  [MACD/ORB] [KTrade
 [Fib]     [Candle]    Risk Engine]
    │                     │
[State                [Bracket
 Store]               Orders → Alpaca]
```

**Golden Rule:** AI generates signals. Hard-coded math decides if it's safe.

---

## Developer Feedback Status

| Issue | Status |
|-------|--------|
| yfinance dependency | ✅ FIXED → Polygon.io |
| Synchronous monolith | ✅ PARTIALLY FIXED → CEO agents + StateStore |
| Survivorship bias | ✅ ADDRESSED → warnings + dead pool |
| Async architecture | 🔄 v10 roadmap |
| PostgreSQL state | 🔄 v10 roadmap |
| Full market backtest | 🔄 v10 roadmap |

---

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| v8.1 | Jun 10, 2026 | MACD/EMA, ORB, Heartbeat, ConvictionScorer |
| v8.3 | Jun 15, 2026 | CEO orchestrator, signal_is_fresh, cost guards |
| v9.0 | Jun 15, 2026 | Polygon.io, Fib signals, StateStore, dev fixes |

---

*KTrade PRO is for paper trading and educational purposes.
Not financial advice. Always test before using real capital.*

## Windows Safe Start

Read-only strategy scan using `agent/ktrade_agent_v9.py` with Polygon daily bars:

```powershell
.\run-score-only.ps1
```

Start the Alpaca paper-account bridge:

```powershell
.\start-paper-bridge.ps1
```

The bridge runs at `http://localhost:5001`. Buy and sell endpoints are disabled
by default with `KTRADE_PAPER_ORDER_SUBMISSION=false`.

Latest agent scan:

```text
http://localhost:5001/agent/status
```

Stop the bridge:

```powershell
.\stop-paper-bridge.ps1
```


## KTrade-only package

This package excludes External Portfolio bridge/app-specific files. Use KTrade for scanning, risk validation, and broker execution workflows.


## KTrade V12.2 local runtime

V12.2 adds KTrade-only local runtime supervision:

```bash
python scripts/run_ktrade_local.py --open-browser
```

This keeps mutable runtime files outside the source folder when configured, starts the backend on `127.0.0.1`, health-checks `/health`, and exposes `/runtime/status`.

Safe defaults remain:

```env
KTRADE_PAPER_ORDER_SUBMISSION=false
LIVE_TRADING=false
KTRADE_MANUAL_ALLOW_DEMO=false
```

See `docs/KTRADE_LOCAL_RUNTIME.md` and `V12.2_KTRADE_RUNTIME_MERGE.md`.
