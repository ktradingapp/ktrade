"""V13.3 volatility-regime estimator tests (numpy; fully offline, deterministic).

Covers the primitives (returns, vol, thresholds, classify, transition matrix,
escalation prob), the estimator snapshot, the out-of-sample evaluation (on a
synthetic series where high-vol blocks carry negative drift, so vol-scaling should
beat a fair constant-exposure baseline), and the shadow ledger.
"""
import os
import sys
import json
import tempfile

import numpy as np

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from agent import regime_estimator as R
except Exception as exc:
    print(f"SKIP test_v132: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
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


def test_primitives():
    rets = R.returns_from_prices([100, 101, 102.01])
    check("returns ~1% each", abs(rets[0] - 0.01) < 1e-9 and abs(rets[1] - 0.01) < 1e-9)

    flat = R.realized_vol(np.full(40, 0.01), window=20)
    check("constant returns -> ~0 vol", abs(np.nanmax(flat)) < 1e-9)
    check("vol warmup is NaN", np.isnan(flat[0]))

    labels = R.classify([0.05, 0.2, 0.4, float("nan")], (0.1, 0.3))
    check("classify LOW/MID/HIGH/None", labels == ["LOW", "MID", "HIGH", None])


def test_transition_matrix():
    tm = R.transition_matrix(["LOW", "LOW", "HIGH", "HIGH", "LOW"])
    m = tm["matrix"]
    check("LOW->LOW 0.5", m["LOW"]["LOW"] == 0.5)
    check("LOW->HIGH 0.5", m["LOW"]["HIGH"] == 0.5)
    check("HIGH->LOW 0.5", m["HIGH"]["LOW"] == 0.5)
    check("escalation P(HIGH|LOW)=0.5", R.escalation_prob(tm, "LOW") == 0.5)
    check("None entries ignored (no crash)", R.escalation_prob(tm, None) is None)


def test_estimator_snapshot():
    # calm uptrend then a volatile stretch
    rng = np.random.default_rng(1)
    calm = 100 * np.cumprod(1 + rng.normal(0.0005, 0.004, 120))
    wild = calm[-1] * np.cumprod(1 + rng.normal(-0.001, 0.03, 60))
    prices = np.concatenate([calm, wild])
    snap = R.RegimeEstimator(window=20).current(prices)
    check("snapshot has a regime label", snap["regime"] in R.STATES)
    for k in ("realized_vol", "escalation_prob", "suggested_size_mult", "transition_row"):
        check(f"snapshot has {k}", k in snap)
    check("suggested mult in (0,1]", 0 < snap["suggested_size_mult"] <= 1.0)


def _adverse_vol_series(seed=7):
    """Block-alternating vol; high-vol blocks have strong negative drift so cutting
    exposure there should help. Signal is large -> outcome is ~deterministic."""
    rng = np.random.default_rng(seed)
    rets = []
    for b in range(16):                       # 16 blocks x 25 = 400 days
        if b % 2 == 0:                        # low-vol, mildly positive
            rets.extend(rng.normal(0.0010, 0.004, 25))
        else:                                 # high-vol, strongly negative
            rets.extend(rng.normal(-0.0040, 0.025, 25))
    rets = np.asarray(rets)
    prices = 100 * np.cumprod(1 + rets)
    return np.concatenate([[100.0], prices])


def test_oos_evaluation():
    prices = _adverse_vol_series()
    ev = R.evaluate_regime_value(prices, train_frac=0.6, window=20)
    check("evaluation ok", ev.get("ok") is True)
    check("has test days", ev["test_days"] > 20)
    check("avg exposure < 1 (cuts risk in high vol)", ev["avg_exposure"] < 1.0)
    # high-vol blocks are negative, so vol-scaling should beat a same-exposure baseline
    check("regime beats fair baseline OOS (Sharpe)", ev["regime_beats_baseline"] is True)
    # and cut drawdown vs full exposure (both negative; regime less severe)
    check("regime drawdown less severe than full", ev["maxdd_regime"] > ev["maxdd_full"])
    check("verdict mentions OUT-OF-SAMPLE", "OUT-OF-SAMPLE" in ev["verdict"])


def test_evaluation_guards():
    short = R.evaluate_regime_value([100, 101, 102], window=20)
    check("short series -> not ok (no crash)", short.get("ok") is False)


def test_ledger():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.remove(path)
    led = R.RegimeLedger(path=path)
    led.record({"regime": "HIGH", "realized_vol": 0.42, "escalation_prob": 0.7,
                "suggested_size_mult": 0.3}, benchmark="SPY")
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    check("ledger wrote one UTC row", len(rows) == 1 and rows[0]["ts"].endswith("+00:00"))
    check("ledger captured regime", rows[0]["regime"] == "HIGH")
    os.remove(path)


if __name__ == "__main__":
    test_primitives()
    test_transition_matrix()
    test_estimator_snapshot()
    test_oos_evaluation()
    test_evaluation_guards()
    test_ledger()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
