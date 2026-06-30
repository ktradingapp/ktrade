"""v10.6 fix validation: equity baseline, no-reference BUY guard, ref seeding."""
import sys, os, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import numpy as np, pandas as pd
spec = importlib.util.spec_from_file_location("ktrade_agent_v9", os.path.join(HERE, "agent", "ktrade_agent_v9.py"))
A = importlib.util.module_from_spec(spec); sys.modules[spec.name] = A; spec.loader.exec_module(A)

P, F = [], []
def check(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ")+n)

def daily(n=260, seed=1):
    rng = np.random.default_rng(seed); r = rng.normal(0.003, 0.01, n); c = 100*np.cumprod(1+r)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": c, "high": c*1.01, "low": c*0.99, "close": c,
                         "volume": rng.integers(1e6, 5e6, n).astype(float)}, index=idx)

# --- No-reference BUY guard (default on) ---
eng = A.RiskEngine()
unseeded = A.TradeRequest(ticker="NEWX", side="buy", qty=0, price=2300.52,
                          conviction=90, atr=5.0, desired_risk_dollars=500)
d = eng.evaluate(unseeded)
check("v10.6 first BUY with no reference is BLOCKED", (not d.approved) and "NO PRICE REFERENCE" in d.reason)

eng.seed_references({"NEWX": 230.0})
d2 = eng.evaluate(A.TradeRequest(ticker="NEWX", side="buy", qty=0, price=231.0,
                                 conviction=90, atr=5.0, desired_risk_dollars=500))
check("v10.6 BUY approved once reference seeded", d2.approved and d2.approved_qty > 0)

# seeding a bad decimal-shift still blocked as bad price
d3 = eng.evaluate(A.TradeRequest(ticker="NEWX", side="buy", qty=0, price=2300.52,
                                 conviction=90, atr=5.0, desired_risk_dollars=500))
check("v10.6 bad decimal-shift still blocked with reference", (not d3.approved) and "BAD PRICE" in d3.reason)

# --- Equity baseline from broker (no false daily-loss halt) ---
class FakeBroker:
    def __init__(self, equity): self.equity = equity
    def get_account(self): return {"equity": self.equity}
    def get_positions(self): return []
    def submit_bracket(self, *a, **k): return {"id": "o", "client_order_id": k.get("client_order_id")}
    def await_fill(self, o, timeout_s=None): return {"filled_qty": 1, "filled_avg_price": 100.0, "status": "filled"}

A.CFG.account_value = 100000.0          # config says 100k
ceo = A.KTradeCEO(broker=FakeBroker(equity=50000.0))   # broker truth says 50k
dm = {"NVDA": daily(seed=1), "SPY": daily(seed=2)}
prices = {t: float(df["close"].iloc[-1]) for t, df in dm.items()}
res = ceo.run_cycle(dm, prices)
check("v10.6 equity baseline set to broker truth (50k, not 100k)",
      abs(ceo.risk.engine.equity_open - 50000.0) < 1e-6)
check("v10.6 no false daily-loss halt on real equity != ACCOUNT_VALUE",
      ceo.loss_guard.triggered is False and ceo._equity_initialized is True)

# --- Ref seeding from data_map ---
ceo2 = A.KTradeCEO(broker=FakeBroker(equity=100000.0))
ceo2._seed_price_references_from_data(dm)
check("v10.6 references seeded from data_map prior close",
      "NVDA" in ceo2.risk.engine.price_refs and ceo2.risk.engine.price_refs["NVDA"] > 0)

print("\n==== %d passed, %d failed ====" % (len(P), len(F)))
if F: print("FAILED:", F); sys.exit(1)
