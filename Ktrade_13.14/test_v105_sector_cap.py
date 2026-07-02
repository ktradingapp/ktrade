"""v10.5 sector/correlation concentration cap tests. Run from project root."""
import sys, os, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("ktrade_agent_v9", os.path.join(HERE, "agent", "ktrade_agent_v9.py"))
A = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = A
spec.loader.exec_module(A)

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name); print(("PASS " if cond else "FAIL ") + name)

# sector mapping
check("sector_of NVDA -> AI_SEMI", A.sector_of("NVDA") == "AI_SEMI")
check("sector_of GOOGL -> AI_CLOUD_SW", A.sector_of("GOOGL") == "AI_CLOUD_SW")
check("sector_of SPY -> INDEX_ETF", A.sector_of("SPY") == "INDEX_ETF")
check("sector_of unknown -> OTHER", A.sector_of("ZZZZ") == "OTHER")

def fresh_engine():
    e = A.RiskEngine()
    e.seed_references({t: 100.0 for t in
        ["NVDA","AMD","MU","INTC","AVGO","GOOGL","META","AMZN","ZZZZ","YYYY","XXXX"]})
    return e

req = lambda tkr: A.TradeRequest(ticker=tkr, side="buy", qty=0, price=100.0,
                                 conviction=90, atr=2.0, desired_risk_dollars=300)

# --- position-count cap: 3 AI_SEMI longs (small value), 4th blocked on count ---
e = fresh_engine()
for t in ["NVDA", "AMD", "MU"]:
    e.record_fill(t, "buy", 10, 100.0)   # $1k each -> 3% total, well under 30%
d = e.evaluate(req("INTC"))
check("count cap: 4th AI_SEMI blocked (SECTOR CAP)",
      (not d.approved) and "SECTOR CAP" in d.reason)

# cross-sector is fine even with 3 AI_SEMI open
d2 = e.evaluate(req("GOOGL"))
check("cross-sector buy allowed (different basket)", d2.approved)

# --- dollar-exposure cap: 2 big AI_SEMI ($30k), next AI_SEMI over 30% ---
e2 = fresh_engine()
e2.record_fill("NVDA", "buy", 150, 100.0)  # $15k
e2.record_fill("AMD",  "buy", 150, 100.0)  # $15k -> $30k, only 2 positions
d3 = e2.evaluate(req("MU"))
check("exposure cap: AI_SEMI over 30% blocked (SECTOR EXPOSURE CAP)",
      (not d3.approved) and "EXPOSURE CAP" in d3.reason)

# --- OTHER tickers are exempt from the sector cap ---
e3 = fresh_engine()
for t in ["ZZZZ", "YYYY", "XXXX"]:
    e3.record_fill(t, "buy", 10, 100.0)
d4 = e3.evaluate(req("ZZZZ"))  # OTHER -> not sector-blocked (may add to position)
check("OTHER tickers exempt from sector cap", "SECTOR" not in d4.reason)

# --- sanity: a single AI_SEMI buy on an empty book is approved ---
e4 = fresh_engine()
d5 = e4.evaluate(req("NVDA"))
check("single AI_SEMI buy approved on empty book", d5.approved)

print("\n==== %d passed, %d failed ====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
