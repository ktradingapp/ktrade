# KTrade PRO — Risk Rules & Configuration Guide
### The Golden Rule: Never Trust the AI With the Emergency Brakes

---

## Philosophy

The KTrade risk engine sits BETWEEN the AI and the broker.
The AI generates signals. The risk engine decides if it's safe to act.
If ANY check fails → trade is BLOCKED. No exceptions.

```
AI Signal → Risk Engine → APPROVED/BLOCKED → Broker
               ↑
          This layer is
          pure math only.
          No ML. No AI.
```

---

## Current Risk Parameters

> ⚠️ **v10.8 correction:** the *live* autonomous agent reads its config from
> `class CFG` in `agent/ktrade_agent_v9.py` (overridable via `.env`), **not**
> from `risk/ktrade_risk.py`. The values below match both, but editing
> `risk/ktrade_risk.py::RiskConfig` alone will NOT change live behavior —
> set the matching `.env` keys or edit `CFG`. `risk/ktrade_risk.py` is a
> standalone reference module.

Edit these in `agent/ktrade_agent_v9.py` → `class CFG` (or via `.env`)

### Kill Switch
| Parameter | Default | Description |
|-----------|---------|-------------|
| KILL_SWITCH_ACTIVE | False | Manually flip to True to halt all trading immediately |
| MAX_DAILY_DRAWDOWN_PCT | 3.0% | Auto-activates kill switch if account drops 3% in a day |
| MAX_DAILY_LOSS_DOLLARS | $3,000 | Absolute dollar loss cap per day |
| MAX_OPEN_POSITIONS | 10 | Never hold more than 10 positions simultaneously |

### Per-Trade Limits
| Parameter | Default | Description |
|-----------|---------|-------------|
| MAX_POSITION_SIZE_PCT | 10% | No single position > 10% of account |
| MAX_TRADE_DOLLAR_RISK | $1,000 | Max dollar risk per trade (stop distance × qty) |
| MIN_CONVICTION_SCORE | 75 | Reject any signal below 75 conviction |

### Duplicate Order Prevention (NEW in v1.0)
| Parameter | Default | Description |
|-----------|---------|-------------|
| DUPLICATE_WINDOW_SECONDS | 60 | Block same ticker+side within 60 seconds |
| MAX_SAME_TICKER_PER_DAY | 3 | Max entries in same ticker per day |
| TICKER_COOLDOWN_SECONDS | 300 | 5 min mandatory wait after any fill |

### VIX Circuit Breakers
| Parameter | Default | Description |
|-----------|---------|-------------|
| HIGH_VIX | 25.0 | Reduce position size 50% if VIX > 25 |
| AUTO_HEDGE_VIX | 28.0 | Warn to add PUT hedge if VIX > 28 |
| VIX_RISK_OFF_THRESHOLD | 30.0 | Pause all new longs if VIX > 30 |
| VIX_CLOSE_ALL_THRESHOLD | 50.0 | Close ALL positions if VIX > 50 (emergency) |

### Flash Crash Detection
| Parameter | Default | Description |
|-----------|---------|-------------|
| FLASH_CRASH_DROP_PCT | 2.5% | Halt if SPY drops 2.5% in < 10 minutes |
| MAX_BID_ASK_SPREAD_PCT | 0.5% | Reject options with spread > 0.5% of price |
| MIN_OPTION_VOLUME | 100 | Reject options with < 100 daily volume |

### Position Sizing
| Parameter | Default | Description |
|-----------|---------|-------------|
| ATR_RISK_MULTIPLIER | 1.5× | Stop = entry − (ATR × 1.5) |
| DEFAULT_STOP_PCT | 2.0% | Default stop if no ATR available |
| DEFAULT_TARGET_PCT | 4.0% | Default take-profit (2:1 R/R) |
| KELLY_WIN_RATE | 0.55 | Your historical win rate (update regularly!) |
| KELLY_AVG_WIN | 1.8 | Average win / average loss ratio |
| KELLY_FRACTION | 0.25 | Use 25% of full Kelly (conservative) |

### Short / Hedge
| Parameter | Default | Description |
|-----------|---------|-------------|
| ALLOW_SHORTS | True | Allow short selling as hedge |
| MAX_SHORT_EXPOSURE_PCT | 15% | Total short exposure cap |

---

## How to Tune for Your Account Size

### $10,000 account (aggressive paper trading)
```python
MAX_DAILY_LOSS_DOLLARS   = 300     # 3% of account
MAX_TRADE_DOLLAR_RISK    = 100     # 1% per trade
MAX_POSITION_SIZE_PCT    = 15.0    # allow bigger positions
```

### $25,000 account (standard day trading)
```python
MAX_DAILY_LOSS_DOLLARS   = 750
MAX_TRADE_DOLLAR_RISK    = 250
MAX_POSITION_SIZE_PCT    = 10.0
```

### $100,000 account (default config)
```python
MAX_DAILY_LOSS_DOLLARS   = 3000
MAX_TRADE_DOLLAR_RISK    = 1000
MAX_POSITION_SIZE_PCT    = 10.0
```

---

## Bracket Orders Explained

Every approved trade automatically sends a bracket order to Alpaca:

```
BUY 10x NVDA @ $131.20
  ├── Stop Loss:   $126.40  (broker-side, executes even if agent is offline)
  └── Take Profit: $140.80  (broker-side, executes even if agent is offline)
```

**Why this matters:** If your machine crashes, internet goes down, or the AI
freezes — Alpaca's servers still execute your stop and target.
The AI does NOT need to be running to exit the trade safely.

---

## The 3 Bugs We Fixed (from real agent observation Jun 15, 2025)

### Bug 1: LITE traded 4× at same timestamp
**Root cause:** Agent sent duplicate orders within milliseconds
**Our fix:** 60-second duplicate window per ticker+side
**Result:** Only 1st order passes; orders 2-4 are blocked instantly

### Bug 2: APLD 3 shares then 111 shares immediately
**Root cause:** Agent double-fired after partial fill confirmation
**Our fix:** 5-minute mandatory cooldown after any confirmed fill
**Result:** Re-entry blocked with "Wait 299s" message

### Bug 3: No shorts/hedges on down days
**Root cause:** Agent only had BUY logic
**Our fix:** Short tracking + auto-hedge warning when VIX > 28
**Result:** Agent suggests PUT hedge when market gets volatile

---

## Emergency Procedures

### Manual Kill Switch (immediate halt)
```python
engine.activate_kill_switch("Manual override — market conditions")
```

### Reset for new trading day
```python
engine.reset_kill_switch()
engine.equity_open = current_account_equity  # reset daily baseline
```

### Check engine status anytime
```python
print(engine.status())
# Returns: kill_active, vix, daily_pnl, drawdown, positions, etc.
```
