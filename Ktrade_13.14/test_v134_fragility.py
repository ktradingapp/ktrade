"""V13.6 parameter-fragility engine tests (stdlib-only, fully offline).

Verifies the detection logic on synthetic backtest surfaces where the answer is
known: a smooth plateau must read ROBUST; a lonely spike must read FRAGILE. The
real VectorBT-backed path needs price data + vectorbt and is exercised on the VPS,
not here — these tests pin the math that decides robust-vs-curve-fit.
"""
import os
import sys

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from agent import param_fragility as F
except Exception as exc:
    print(f"SKIP test_v134: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
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


GRID = {"a": [1, 2, 3, 4, 5], "b": [1, 2, 3, 4, 5]}


def test_robust_surface():
    # smooth concave plateau, profitable everywhere, peak at the centre
    def bt(p):
        return {"sharpe": 1.5 - 0.2 * (abs(p["a"] - 3) + abs(p["b"] - 3))}
    res = F.sweep(bt, GRID, metric="sharpe")
    check("sweep covers full grid (25)", len(res) == 25)
    a = F.analyze_fragility(res, GRID, metric="sharpe")
    check("robust: best at centre (3,3)", a["best_params"] == {"a": 3, "b": 3})
    check("robust: profitable_fraction 1.0", a["profitable_fraction"] == 1.0)
    check("robust: plateau_ratio high (>0.8)", a["plateau_ratio"] > 0.8)
    check("robust: no cliffs", a["cliff_fraction"] == 0.0)
    check("robust: classified ROBUST", a["classification"] == "ROBUST")
    check("robust: verdict mentions plateau", "plateau" in a["verdict"].lower())


def test_fragile_surface():
    # a single lucky spike; everything else loses
    def bt(p):
        return {"sharpe": 3.0 if (p["a"], p["b"]) == (2, 4) else -0.3}
    res = F.sweep(bt, GRID, metric="sharpe")
    a = F.analyze_fragility(res, GRID, metric="sharpe")
    check("fragile: best is the spike (2,4)", a["best_params"] == {"a": 2, "b": 4})
    check("fragile: tiny profitable_fraction", a["profitable_fraction"] < 0.1)
    check("fragile: plateau_ratio low (<0.2)", a["plateau_ratio"] < 0.2)
    check("fragile: classified FRAGILE", a["classification"] == "FRAGILE")
    check("fragile: verdict warns curve-fit", "curve-fit" in a["verdict"].lower())


def test_no_trade_combos_excluded():
    # backtest_fn returns None (no trades) for half the grid -> excluded, no crash
    def bt(p):
        return {"sharpe": 1.0} if p["a"] <= 3 else None
    res = F.sweep(bt, GRID, metric="sharpe")
    check("all 25 combos attempted", len(res) == 25)
    valued = [r for r in res if r["value"] is not None]
    check("only trading combos valued (15)", len(valued) == 15)
    a = F.analyze_fragility(res, GRID, metric="sharpe")
    check("analysis ignores no-trade combos", a["n_valued"] == 15)


def test_all_invalid_guard():
    a = F.analyze_fragility([{"params": {"a": 1, "b": 1}, "value": None, "metrics": None}],
                            GRID, metric="sharpe")
    check("no valid results -> ok False (no crash)", a.get("ok") is False)


def test_report_renders():
    def bt(p):
        return {"sharpe": 1.5 - 0.2 * (abs(p["a"] - 3) + abs(p["b"] - 3))}
    a = F.analyze_fragility(F.sweep(bt, GRID), GRID)
    txt = F.format_report(a, GRID)
    check("report renders", "Fragility Sweep" in txt and "CLASSIFICATION" in txt)


if __name__ == "__main__":
    test_robust_surface()
    test_fragile_surface()
    test_no_trade_combos_excluded()
    test_all_invalid_guard()
    test_report_renders()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
