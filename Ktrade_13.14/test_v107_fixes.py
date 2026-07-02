"""v10.7 fix validation: schema, partial fills, emergency, state store, daily reset, kill persistence."""
import sys, os, json, tempfile, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import numpy as np, pandas as pd

def load(modpath, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, modpath))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m

A = load("agent/ktrade_agent_v9.py", "ktrade_agent_v9")
SV = load("data/schema_validation.py", "schema_validation")
PF = load("risk/position_fills.py", "position_fills")
EM = load("risk/emergency.py", "emergency")
SS = load("risk/state_store.py", "state_store")
SC = load("data/scan_schema.py", "scan_schema")
MO = load("backend/manual_order_schema.py", "manual_order_schema")

P, F = [], []
def check(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ")+n)

# #5 schema validation: uppercase cols + string numbers + bad rows cleaned
raw = pd.DataFrame({
    "Open": ["100", "101", "102"]*20, "High": [103]*60, "Low": [99]*60,
    "Close": [100.5]*60, "Volume": [1000]*60,
}, index=pd.date_range("2025-01-01", periods=60, freq="D"))
clean = SV.normalize_ohlcv_frame("NVDA", raw, "1d", min_rows=50)
check("#5 normalize handles uppercase + string numbers", list(clean.columns) == ["open","high","low","close","volume"] and len(clean) == 60)
try:
    SV.normalize_ohlcv_frame("X", pd.DataFrame({"close":[1,2]}), "1d", 50); ok=False
except ValueError: ok=True
check("#5 normalize raises on missing columns", ok)

# #8 drop unclosed last bar (recent timestamp)
idx = pd.date_range(pd.Timestamp.now("UTC").normalize(), periods=3, freq="D")
df = pd.DataFrame({"open":[1,1,1],"high":[1,1,1],"low":[1,1,1],"close":[1,1,1],"volume":[1,1,1]}, index=idx)
check("#8 drop_unclosed_last_bar removes forming bar", len(SV.drop_unclosed_last_bar(df, "1d")) == 2)

# #9 partial fills
pos = {}
PF.apply_fill_to_position(pos, "NVDA", "buy", 10, 100.0)
PF.apply_fill_to_position(pos, "NVDA", "buy", 10, 110.0)
check("#9 buy blends avg cost", abs(pos["NVDA"]["avg_cost"] - 105.0) < 1e-6 and pos["NVDA"]["qty"] == 20)
PF.apply_fill_to_position(pos, "NVDA", "sell", 5, 120.0)
check("#9 partial sell reduces qty (keeps position)", pos["NVDA"]["qty"] == 15 and "NVDA" in pos)
PF.apply_fill_to_position(pos, "NVDA", "sell", 15, 120.0)
check("#9 full sell removes position", "NVDA" not in pos)

# #2 emergency controller: persistent kill + cancel/flatten
class FakeBroker:
    def __init__(self): self.cancelled=False; self.flattened=False
    def cancel_all_orders(self): self.cancelled=True
    def close_all_positions(self): self.flattened=True
with tempfile.TemporaryDirectory() as d:
    kf = os.path.join(d, "kill.json")
    fb = FakeBroker()
    ec = EM.EmergencyController(broker=fb, state_file=kf)
    ec.trigger("VIX emergency", flatten=True)
    check("#2 emergency cancels orders + flattens", fb.cancelled and fb.flattened and ec.active())
    ec2 = EM.EmergencyController(broker=FakeBroker(), state_file=kf)
    check("#2 kill state PERSISTS across restart", ec2.active() and "VIX" in ec2.reason)
    ec2.reset()
    ec3 = EM.EmergencyController(broker=FakeBroker(), state_file=kf)
    check("#2 reset clears persisted kill", not ec3.active())

# #3 state store round-trip
with tempfile.TemporaryDirectory() as d:
    st = SS.RiskStateStore(os.path.join(d, "rs.json"))
    st.save({"approved_today": 7, "kill_active": True})
    loaded = st.load()
    check("#3 state store persists + reloads", loaded.get("approved_today") == 7 and loaded.get("kill_active") is True)

# #1 manual order schema
ok_order, err = MO.parse_manual_order({"ticker":"nvda","side":"buy","qty":3,"type":"market"})
check("#1 valid manual order parsed + uppercased", err is None and ok_order["ticker"] == "NVDA")
bad, err2 = MO.parse_manual_order({"ticker":"!!!","side":"buy","qty":3})
check("#1 invalid ticker rejected", bad is None and err2)
bad2, err3 = MO.parse_manual_order({"ticker":"NVDA","side":"buy","qty":-5})
check("#1 non-positive qty rejected", bad2 is None and err3)
bad3, err4 = MO.parse_manual_order({"ticker":"NVDA","side":"buy","qty":1,"type":"limit"})
check("#1 limit order without price rejected", bad3 is None and err4)

# #6 scan schema validation
good = {"generated_at":"t","results":[{"ticker":"nvda","action":"BUY","conviction":80,"price":100.0}]}
v = SC.validate_scan_dict(good)
check("#6 valid scan payload passes + uppercases ticker", v["results"][0]["ticker"] == "NVDA")
bad_scan = {"generated_at":"t","results":[{"ticker":"NVDA","action":"BUY","conviction":80,"price":-5}]}
v2 = SC.validate_scan_dict(bad_scan)
check("#6 invalid scan (negative price) -> safe empty", v2["results"] == [] and v2["errors"])

# #4 daily reset + kill persistence wired into CEO (uses broker)
class FB2:
    def get_account(self): return {"equity": 100000.0}
    def get_positions(self): return []
    def cancel_all_orders(self): pass
    def close_all_positions(self): pass
ceo = A.KTradeCEO(broker=FB2())
import datetime as _dt
ceo._risk_day = _dt.date(2020,1,1)   # force stale day
ceo._roll_trading_day_if_needed(100000.0)
check("#4 daily rollover resets risk day", ceo._risk_day == _dt.date.today() and ceo.risk.engine.approved_today == 0)
check("v10.7 RiskEngine has emergency + state_store wired", ceo.risk.engine.emergency is not None and ceo.risk.engine.state_store is not None)

print("\n==== %d passed, %d failed ====" % (len(P), len(F)))
if F: print("FAILED:", F); sys.exit(1)
