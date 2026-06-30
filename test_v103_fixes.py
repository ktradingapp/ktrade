"""v10.3 fix validation. Run from project root."""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
import pandas as pd

import importlib.util
spec = importlib.util.spec_from_file_location("ktrade_agent_v9", os.path.join(HERE, "agent", "ktrade_agent_v9.py"))
A = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = A
spec.loader.exec_module(A)

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)

# Confirm the price guard actually loaded (else the gate is a no-op).
check("PRICE_GUARD loaded into agent", A.PRICE_GUARD is not None)

def uptrend(n=260, start=100.0, drift=0.004, vol=0.01, seed=0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low  = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume}, index=idx)

# -------------------------------------------------------------------------
# F2: bad-tick decimal-shift is blocked at the RiskEngine final gate.
# -------------------------------------------------------------------------
eng = A.RiskEngine()
eng.seed_references({"KLAC": 236.0})
bad = A.TradeRequest(ticker="KLAC", side="buy", qty=0, price=2300.52,
                     conviction=90, atr=5.0, desired_risk_dollars=500)
d_bad = eng.evaluate(bad)
check("F2 bad KLAC tick BLOCKED at RiskEngine", (not d_bad.approved) and "BAD PRICE" in d_bad.reason)

good = A.TradeRequest(ticker="KLAC", side="buy", qty=0, price=238.0,
                      conviction=90, atr=5.0, desired_risk_dollars=500)
d_good = eng.evaluate(good)
check("F2 good KLAC price approved", d_good.approved and d_good.approved_qty > 0)

# -------------------------------------------------------------------------
# Sole sizer: qty=0 in, RiskEngine returns a real size from risk budget.
# -------------------------------------------------------------------------
check("Sizer: RiskEngine sizes from budget (qty=0 in -> qty>0 out)", d_good.approved_qty > 0)
# risk budget respected: dollar_risk should not exceed the cap
check("Sizer: dollar risk within max_trade_dollar_risk",
      d_good.dollar_risk <= A.CFG.max_trade_dollar_risk + 1e-6)

# -------------------------------------------------------------------------
# F3: record_fill populates open_positions; MAX POSITIONS then enforced.
# -------------------------------------------------------------------------
eng2 = A.RiskEngine()
eng2.seed_references({f"T{i}": 100.0 for i in range(12)})
# v10.6: BUYs now require a price reference; seed TX so this test still reaches the MAX POSITIONS path
eng2.seed_references({"TX": 100.0})
for i in range(A.CFG.max_open_positions):
    eng2.record_fill(f"T{i}", "buy", 10, 100.0)
check("F3 open_positions tracked after buys",
      len(eng2.open_positions) == A.CFG.max_open_positions)
over = A.TradeRequest(ticker="TX", side="buy", qty=0, price=100.0,
                      conviction=90, atr=2.0, desired_risk_dollars=300)
d_over = eng2.evaluate(over)
check("F3 MAX POSITIONS now enforced", (not d_over.approved) and "MAX POSITIONS" in d_over.reason)

# sync_positions overwrites with broker truth
eng2.sync_positions([{"ticker": "AAA", "shares": 5, "avgCost": 10.0, "currentPrice": 10.0}])
check("F3 sync_positions replaces with broker truth", list(eng2.open_positions) == ["AAA"])

# -------------------------------------------------------------------------
# F1: momentum is an executable strategy signal.
# -------------------------------------------------------------------------
sa = A.StrategyAgent()
df_up = uptrend(seed=1)
sigs = sa.run("NVDA", df_up, intraday=False)
check("F1 StrategyAgent emits momentum/trend keys",
      "momentum" in sigs and "trend" in sigs and "orb" not in sigs)
check("F1 momentum fires on clean uptrend", sigs["momentum"] == 1)

# -------------------------------------------------------------------------
# F6: scorer is timeframe-aware -> no ORB component / label on daily bars.
# -------------------------------------------------------------------------
sc = A.ConvictionScorer()
s_daily = sc.score("NVDA", df_up, interval="1d")
check("F6 daily score has no orb component", "orb" not in s_daily.components)
check("F6 daily strategy label is never ORB", s_daily.strategy != "ORB")
s_intra = sc.score("NVDA", df_up, interval="5m")
check("F6 intraday score includes orb component", "orb" in s_intra.components)

# -------------------------------------------------------------------------
# F4/F5: ExecutionAgent never phantom-fills without a broker; records real fill.
# -------------------------------------------------------------------------
dec = A.RiskDecision(approved=True, reason="ok", ticker="NVDA", side="buy",
                     original_qty=0, approved_qty=10, stop_price=95, target_price=110)
score = A.ConvictionScore(ticker="NVDA", score=80, price=100.0, atr=2.0)
ex_sim = A.ExecutionAgent(broker=None)
r_sim = ex_sim.execute(dec, score)
check("F4 no-broker execute does NOT report a fill", r_sim["filled"] is False and r_sim["simulated"] is True)

class FakeBroker:
    def __init__(self, fill=True): self.fill = fill
    def get_account(self): return {"equity": 100000.0}
    def get_positions(self): return []
    def submit_bracket(self, t, q, s, stop, tgt, client_order_id=None):
        return {"id": "o1", "client_order_id": client_order_id}
    def await_fill(self, order, timeout_s=None):
        return {"filled_qty": 10, "filled_avg_price": 100.5, "status": "filled"} if self.fill \
               else {"filled_qty": 0, "filled_avg_price": 0, "status": "canceled"}

ex_real = A.ExecutionAgent(broker=FakeBroker(fill=True))
r_real = ex_real.execute(dec, score)
check("F5 confirmed broker fill recorded", r_real["filled"] is True and r_real["qty"] == 10)
ex_unfilled = A.ExecutionAgent(broker=FakeBroker(fill=False))
r_unf = ex_unfilled.execute(dec, score)
check("F4 unfilled order -> no fill recorded", r_unf["filled"] is False)

# -------------------------------------------------------------------------
# F12: cost optimizer counts per-ticker calls, not 1.
# -------------------------------------------------------------------------
co = A.CostOptimizerAgent()
co.record_calls(count=100, cost=1.0)
check("F12 record_calls counts 100, not 1", co._calls_today == 100)

# -------------------------------------------------------------------------
# F10: market hours tz-aware with weekend / holiday awareness.
# -------------------------------------------------------------------------
hb = A.HeartbeatEngine()
import datetime as _dt
sat = _dt.datetime(2026, 6, 20, 12, 0)   # Saturday
hol = _dt.datetime(2026, 12, 25, 12, 0)  # Christmas
wkday = _dt.datetime(2026, 6, 23, 12, 0) # Tuesday
check("F10 weekend not a trading day", hb.is_trading_day(sat) is False)
check("F10 holiday not a trading day", hb.is_trading_day(hol) is False)
check("F10 normal weekday is a trading day", hb.is_trading_day(wkday) is True)

# -------------------------------------------------------------------------
# End-to-end: one run_cycle with a fake broker + SPY flash-crash detection.
# -------------------------------------------------------------------------
ceo = A.KTradeCEO(broker=FakeBroker(fill=True))
data_map = {"NVDA": df_up, "AMD": uptrend(seed=2)}
prices = {t: df["close"].iloc[-1] for t, df in data_map.items()}
res = ceo.run_cycle(data_map, prices)
check("E2E run_cycle returns structured result", isinstance(res, dict) and "trades" in res)

# flash-crash via SPY bar
spy = uptrend(seed=3).copy()
spy.iloc[-1, spy.columns.get_loc("close")] = spy["close"].iloc[-2] * 0.95  # -5% bar
A.MARKET.flash_crash_active = False
ceo2 = A.KTradeCEO(broker=FakeBroker(fill=True))
ceo2._refresh_market_state({"SPY": spy})
check("F9 SPY -5% bar trips flash-crash flag", A.MARKET.flash_crash_active is True)

print("\n==== %d passed, %d failed ====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
