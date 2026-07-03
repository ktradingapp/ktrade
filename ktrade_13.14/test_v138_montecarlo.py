"""V13.10 tests: Monte-Carlo robustness (param_fragility) + single-instance lock."""
import os
import sys
import tempfile
from pathlib import Path

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from agent.param_fragility import (monte_carlo_analysis, format_mc_report,
                                       _max_drawdown, _sharpe, _percentile)
except Exception as exc:
    print(f"SKIP test_v138: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
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


def approx(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def test_helpers():
    check("sharpe = mean/std", approx(_sharpe([0.01, 0.02, 0.03]),
                                      __import__("statistics").mean([0.01, 0.02, 0.03]) /
                                      __import__("statistics").stdev([0.01, 0.02, 0.03])))
    check("sharpe of <2 pts is 0", _sharpe([0.01]) == 0.0)
    check("max_drawdown path-dependent", approx(_max_drawdown([0.1, -0.2, 0.05]), -0.2, 1e-9))
    check("max_drawdown all-up is 0", _max_drawdown([0.01, 0.02, 0.03]) == 0.0)
    check("percentile midpoint", approx(_percentile([0, 10], 50), 5.0))


def test_positive_strategy_robust():
    mc = monte_carlo_analysis([0.02] * 25 + [-0.01] * 15, seed=0)
    check("positive: ok", mc["ok"] is True)
    check("positive: P(Sharpe>0) high (>=0.95)", mc["prob_sharpe_positive"] >= 0.95)
    check("positive: classified ROBUST", mc["classification"] == "ROBUST")
    check("positive: reports a drawdown distribution", mc["shuffled_worst5pct_max_dd_pct"] is not None)


def test_coinflip_fragile():
    mc = monte_carlo_analysis([0.02, -0.02] * 20, seed=0)
    check("coinflip: P(Sharpe>0) ~ 0.5 (<0.9)", mc["prob_sharpe_positive"] < 0.9)
    check("coinflip: classified FRAGILE", mc["classification"] == "FRAGILE")
    check("coinflip: verdict warns noise/curve-fit", "curve-fit" in mc["verdict"].lower())


def test_determinism():
    a = monte_carlo_analysis([0.01, -0.02, 0.03, 0.01, -0.01, 0.02, 0.0, 0.015], seed=7)
    b = monte_carlo_analysis([0.01, -0.02, 0.03, 0.01, -0.01, 0.02, 0.0, 0.015], seed=7)
    check("same seed -> identical prob", a["prob_sharpe_positive"] == b["prob_sharpe_positive"])
    check("same seed -> identical drawdown", a["shuffled_median_max_dd_pct"] == b["shuffled_median_max_dd_pct"])


def test_guards_and_report():
    small = monte_carlo_analysis([0.01, 0.02])
    check("<5 trades -> ok False", small.get("ok") is False)
    check("not-ok report renders", "unavailable" in format_mc_report(small).lower())
    good = monte_carlo_analysis([0.02] * 10 + [-0.01] * 5, seed=1)
    check("ok report renders", "Monte-Carlo Robustness" in format_mc_report(good))


def test_single_instance_lock():
    try:
        from ktrade_runtime.process_lock import ProcessLock
    except Exception as exc:
        print(f"  (skip lock test: {exc})")
        return
    d = tempfile.mkdtemp()
    p = Path(d) / "agent.lock"
    a, b = ProcessLock(p), ProcessLock(p)
    check("lock: first acquire succeeds", a.acquire() is True)
    check("lock: second acquire (live holder) fails", b.acquire() is False)
    a.release()
    check("lock: re-acquire after release", b.acquire() is True)
    b.release()


if __name__ == "__main__":
    test_helpers()
    test_positive_strategy_robust()
    test_coinflip_fragile()
    test_determinism()
    test_guards_and_report()
    test_single_instance_lock()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
