"""v10.9 earnings-awareness tests: blackout gate (BUY) + exit-ahead (holdings)."""
import sys, os, tempfile, importlib.util
from datetime import date, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import numpy as np, pandas as pd

def load(p, n):
    spec = importlib.util.spec_from_file_location(n, os.path.join(HERE, p))
    m = importlib.util.module_from_spec(spec); sys.modules[n] = m; spec.loader.exec_module(m); return m

A = load("agent/ktrade_agent_v9.py", "ktrade_agent_v9")
EC = load("data/earnings_calendar.py", "earnings_calendar")

P, F = [], []
def check(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ")+n)

def cal_with(days_map, tmp):
    """days_map: {ticker: days_until_earnings or None}"""
    def fake(tkr):
        d = days_map.get(tkr.upper(), None)
        return None if d is None else (date.today() + timedelta(days=d)).isoformat()
    return EC.EarningsCalendar(fetch_fn=fake, cache_path=os.path.join(tmp, "ec.json"), ttl_hours=0.0001)

with tempfile.TemporaryDirectory() as tmp:
    cal = cal_with({"MU": 2, "NVDA": 30, "AAPL": None}, tmp)
    blk, ed = cal.in_blackout("MU", 3)
    check("MU within 3d -> blackout", blk is True and ed is not None)
    check("NVDA 30d out -> no blackout", cal.in_blackout("NVDA", 3)[0] is False)
    check("AAPL unknown earnings -> no blackout (fails open)", cal.in_blackout("AAPL", 3)[0] is False)
    check("days_until_earnings MU == 2", cal.days_until_earnings("MU") == 2)

    # RiskEngine BUY blackout gate
    eng = A.RiskEngine()
    eng.earnings_cal = cal
    eng.seed_references({"MU": 100.0, "NVDA": 100.0})
    d_mu = eng.evaluate(A.TradeRequest(ticker="MU", side="buy", qty=0, price=100.0,
                                       conviction=90, atr=2.0, desired_risk_dollars=500))
    check("BUY MU blocked by earnings blackout", (not d_mu.approved) and "EARNINGS BLACKOUT" in d_mu.reason)
    d_nv = eng.evaluate(A.TradeRequest(ticker="NVDA", side="buy", qty=0, price=100.0,
                                       conviction=90, atr=2.0, desired_risk_dollars=500))
    check("BUY NVDA (30d out) NOT blocked by earnings", d_nv.approved)

    # CEO earnings-ahead exit of a holding
    class FB:
        def get_account(self): return {"equity": 100000.0}
        def get_positions(self): return []
        def cancel_all_orders(self): pass
        def close_all_positions(self): pass
    ceo = A.KTradeCEO(broker=FB())
    cal2 = cal_with({"MU": 1, "AVGO": 40}, tmp)
    ceo.earnings_cal = cal2
    ceo.risk.engine.earnings_cal = cal2
    ceo.risk.engine.open_positions = {"MU": {"qty": 10, "avg_cost": 95.0},
                                      "AVGO": {"qty": 5, "avg_cost": 400.0}}
    sold_calls = []
    ceo.execution.monitor.broker_fn = lambda t, s, q: sold_calls.append((t, s, q))
    exits = ceo._earnings_blackout_exits()
    tickers_exited = {e["ticker"] for e in exits}
    check("earnings-ahead exit triggers for MU (1d out)", "MU" in tickers_exited)
    check("earnings-ahead exit does NOT trigger for AVGO (40d out)", "AVGO" not in tickers_exited)
    check("MU exit actually sold via broker hook", ("MU", "sell", 10) in sold_calls)

print("\n==== %d passed, %d failed ====" % (len(P), len(F)))
if F: print("FAILED:", F); sys.exit(1)
