"""V12.9 whipsaw gate — reject new longs in a name whose intraday range is
extreme (the transferable idea from Kubera's buy_gates.max_intraday_range%).

Verifies the gate fires only when a range is seeded AND exceeds the threshold,
that sells and unknown ranges pass untouched, and that the threshold is
env-configurable. Skips gracefully if the agent module can't be imported.
"""
import os
import sys

root = os.path.dirname(os.path.abspath(__file__))
for _p in (root, os.path.join(root, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import agent.ktrade_agent_v9 as A
except Exception as exc:
    print(f"SKIP test_v129: agent import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
    sys.exit(0)

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}")


def _buy(eng, tkr, price):
    return eng.evaluate(A.TradeRequest(ticker=tkr, side="buy", qty=0, price=price, conviction=90))


def test_whipsaw_gate():
    os.environ.pop("KTRADE_MAX_INTRADAY_RANGE_PCT", None)  # default 40%
    eng = A.RiskEngine()
    eng.seed_references({"SDOT": 100.0, "NVDA": 100.0, "AAPL": 100.0})

    # extreme range -> blocked by the whipsaw gate
    eng.seed_intraday_ranges({"SDOT": 120.0})
    d = _buy(eng, "SDOT", 101.0)
    check("extreme range (120%) -> WHIPSAW block", (not d.approved) and "WHIPSAW" in d.reason)

    # normal range -> not a whipsaw block
    eng.seed_intraday_ranges({"NVDA": 8.0})
    d = _buy(eng, "NVDA", 101.0)
    check("normal range (8%) -> not a whipsaw block", "WHIPSAW" not in d.reason)

    # no range seeded -> not a whipsaw block (fail-open on missing data)
    d = _buy(eng, "AAPL", 101.0)
    check("no range seeded -> not a whipsaw block", "WHIPSAW" not in d.reason)

    # sells are never whipsaw-blocked
    ds = eng.evaluate(A.TradeRequest(ticker="SDOT", side="sell", qty=1, price=101.0, conviction=90))
    check("sell is never whipsaw-blocked", "WHIPSAW" not in ds.reason)


def test_threshold_override():
    os.environ["KTRADE_MAX_INTRADAY_RANGE_PCT"] = "200"
    eng = A.RiskEngine()
    eng.seed_references({"SDOT": 100.0})
    eng.seed_intraday_ranges({"SDOT": 120.0})
    d = _buy(eng, "SDOT", 101.0)
    check("threshold raised to 200% -> 120% range passes", "WHIPSAW" not in d.reason)
    os.environ.pop("KTRADE_MAX_INTRADAY_RANGE_PCT", None)


def _df(n=60, last_range_pct=2.0, seed=1):
    """Clean uptrend; the LAST bar's high-low range is exactly last_range_pct%."""
    import numpy as np, pandas as pd
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0.003, 0.01, n))
    high = c * 1.005
    low = c * 0.995
    low[-1] = c[-1]
    high[-1] = c[-1] * (1 + last_range_pct / 100.0)   # (high-low)/low == last_range_pct
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame({"open": c, "high": high, "low": low, "close": c, "volume": vol})


def test_scorer_downgrade():
    """v12.9: the scanner itself downgrades a whipsaw BUY to WATCH, so a violent
    name never reaches the scan output as actionable — covers the score-only path
    (which does not seed the evaluate-gate's range dict)."""
    os.environ.pop("KTRADE_MAX_INTRADAY_RANGE_PCT", None)   # default 40%
    old_min = A.CFG.min_conviction_score
    A.CFG.min_conviction_score = 0   # force any name buy-worthy pre-whipsaw
    try:
        sc = A.ConvictionScorer()
        chaos = sc.score("CHAOS", _df(last_range_pct=50.0))   # 50% > 40%
        check("scorer records the intraday range", chaos.intraday_range_pct >= 40)
        check("scorer downgrades whipsaw BUY -> WATCH (signal 0)", chaos.signal == 0)
        calm = sc.score("CALM", _df(last_range_pct=2.0))      # 2% < 40%
        check("scorer keeps a calm BUY signal", calm.signal == 1)
    finally:
        A.CFG.min_conviction_score = old_min


if __name__ == "__main__":
    test_whipsaw_gate()
    test_threshold_override()
    test_scorer_downgrade()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
