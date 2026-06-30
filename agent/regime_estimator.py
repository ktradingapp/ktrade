#!/usr/bin/env python3
"""KTrade volatility-regime estimator (v13.3) — a SHADOW-ONLY Markov regime layer.

This is the principled, testable version of the "add a Markov regime matrix" idea.
Two deliberate choices keep it honest:

1. Regimes are based on VOLATILITY, not return direction. Volatility clusters
   (high vol begets high vol) is one of the most robust facts in markets; predicting
   return *direction* from a regime is far more fragile and closer to overfitting.
2. The transition matrix is estimated from OBSERVABLE classified states (vol
   tertiles), not latent EM-fit states that relabel themselves on every refit. So it
   stays deterministic and auditable — in keeping with the rest of KTrade.

It changes NOTHING about trading. It emits a regime label + transition probabilities
+ a SUGGESTED size multiplier, all for logging/measurement. Whether to ever act on it
is decided by the out-of-sample test below — not by the fact that it's a matrix.

The headline tool is `evaluate_regime_value()`: fit regime thresholds on a TRAIN
window, then on held-out TEST data compare regime-conditioned sizing against a fair
constant-exposure baseline (same average exposure). If regime sizing does not beat the
dumb baseline out-of-sample, the matrix is decoration.

  python agent/regime_estimator.py --prices spy.csv          # analysis + OOS verdict
  python agent/regime_estimator.py --prices spy.csv --json

Uses numpy (already a dependency). CSV columns: date,close  (or ticker,date,close).
"""
import argparse
import csv
import json
import math
import os
from datetime import datetime, timezone

import numpy as np

STATES = ("LOW", "MID", "HIGH")
_DEFAULT_SIZE_MAP = {"LOW": 1.0, "MID": 0.6, "HIGH": 0.3}
_ANNUALIZE = math.sqrt(252.0)


# --------------------------------------------------------------------------- #
# primitives (pure, testable)
# --------------------------------------------------------------------------- #
def returns_from_prices(prices):
    p = np.asarray(prices, dtype=float)
    if p.size < 2:
        return np.array([])
    return np.diff(p) / p[:-1]


def realized_vol(returns, window=20):
    """Rolling realized vol (annualized). vol[i] uses returns[i-window+1 .. i];
    the first `window-1` entries are NaN. No lookahead when you size return[t]
    with vol[t-1]."""
    r = np.asarray(returns, dtype=float)
    n = r.size
    out = np.full(n, np.nan)
    if n == 0:
        return out
    w = min(window, n)
    for i in range(w - 1, n):
        out[i] = np.std(r[i - w + 1:i + 1], ddof=1) * _ANNUALIZE if w > 1 else 0.0
    return out


def fit_vol_thresholds(vol, quantiles=(0.34, 0.67)):
    """Tertile thresholds from the (train) vol distribution."""
    v = np.asarray(vol, dtype=float)
    v = v[~np.isnan(v)]
    if v.size == 0:
        return (0.0, 0.0)
    lo = float(np.quantile(v, quantiles[0]))
    hi = float(np.quantile(v, quantiles[1]))
    return (lo, hi)


def classify(vol, thresholds):
    """Map a vol series to LOW/MID/HIGH labels (None where vol is NaN)."""
    lo, hi = thresholds
    labels = []
    for v in np.asarray(vol, dtype=float):
        if np.isnan(v):
            labels.append(None)
        elif v <= lo:
            labels.append("LOW")
        elif v >= hi:
            labels.append("HIGH")
        else:
            labels.append("MID")
    return labels


def transition_matrix(labels):
    """Row-normalized empirical transition matrix P(next | current) over STATES."""
    counts = {s: {t: 0 for t in STATES} for s in STATES}
    prev = None
    for lab in labels:
        if lab is None:
            prev = None
            continue
        if prev is not None:
            counts[prev][lab] += 1
        prev = lab
    matrix = {}
    for s in STATES:
        total = sum(counts[s].values())
        matrix[s] = ({t: round(counts[s][t] / total, 4) for t in STATES}
                     if total else {t: None for t in STATES})
    return {"matrix": matrix, "counts": counts}


def escalation_prob(tm, current):
    """P(next == HIGH | current) — a forward 'risk is about to rise' signal."""
    if not current:
        return None
    row = tm.get("matrix", {}).get(current, {})
    return row.get("HIGH")


def _sharpe(rets):
    r = np.asarray(rets, dtype=float)
    r = r[~np.isnan(r)]
    if r.size < 2 or np.std(r, ddof=1) == 0:
        return 0.0
    return float(np.mean(r) / np.std(r, ddof=1) * _ANNUALIZE)


def _max_drawdown(rets):
    r = np.asarray(rets, dtype=float)
    r = r[~np.isnan(r)]
    if r.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    return float(np.min(equity / peak - 1.0))   # most-negative drawdown


# --------------------------------------------------------------------------- #
# estimator
# --------------------------------------------------------------------------- #
class RegimeEstimator:
    def __init__(self, window=20, quantiles=(0.34, 0.67), size_map=None):
        self.window = window
        self.quantiles = quantiles
        self.size_map = dict(size_map or _DEFAULT_SIZE_MAP)
        self.thresholds = None

    def fit(self, prices):
        vol = realized_vol(returns_from_prices(prices), self.window)
        self.thresholds = fit_vol_thresholds(vol, self.quantiles)
        return self

    def label_series(self, prices):
        if self.thresholds is None:
            self.fit(prices)
        vol = realized_vol(returns_from_prices(prices), self.window)
        return classify(vol, self.thresholds), vol

    def current(self, prices):
        """Latest regime + transition row + escalation prob + SUGGESTED size mult
        (suggestion only — not applied anywhere)."""
        labels, vol = self.label_series(prices)
        tm = transition_matrix(labels)
        cur = next((l for l in reversed(labels) if l is not None), None)
        last_vol = next((v for v in reversed(vol) if not np.isnan(v)), None)
        return {
            "regime": cur,
            "realized_vol": round(float(last_vol), 4) if last_vol is not None else None,
            "thresholds": {"low": round(self.thresholds[0], 4),
                           "high": round(self.thresholds[1], 4)},
            "escalation_prob": escalation_prob(tm, cur),
            "transition_row": tm["matrix"].get(cur) if cur else None,
            "suggested_size_mult": self.size_map.get(cur) if cur else None,
        }


# --------------------------------------------------------------------------- #
# the test that actually matters: out-of-sample value vs a fair baseline
# --------------------------------------------------------------------------- #
def evaluate_regime_value(prices, train_frac=0.6, window=20, quantiles=(0.34, 0.67),
                          size_map=None):
    """Fit regime thresholds on the first `train_frac` of history, then on the
    held-out remainder compare three exposure policies on next-day returns (no
    lookahead — regime for day t uses vol through t-1):

      - regime    : size_map[regime] * return
      - const_avg : constant exposure == the regime policy's AVERAGE exposure
                    (the FAIR comparison — isolates regime *timing* from leverage)
      - full      : constant 1.0 exposure

    Verdict is positive only if regime sizing beats const_avg on Sharpe out-of-sample.
    """
    size_map = dict(size_map or _DEFAULT_SIZE_MAP)
    rets = returns_from_prices(prices)
    n = rets.size
    if n < (window + 20):
        return {"ok": False, "reason": f"need >= {window + 20} returns, have {n}"}

    split = int(n * train_frac)
    vol = realized_vol(rets, window)
    train_thr = fit_vol_thresholds(vol[:split], quantiles)

    regime_r, full_r, mults = [], [], []
    for t in range(split, n):
        prev_vol = vol[t - 1]
        if np.isnan(prev_vol):
            continue
        g = classify([prev_vol], train_thr)[0]
        m = size_map.get(g, 1.0)
        regime_r.append(m * rets[t])
        full_r.append(rets[t])
        mults.append(m)

    if len(regime_r) < 10:
        return {"ok": False, "reason": "too few test points after warmup"}

    avg_exposure = float(np.mean(mults))
    const_r = [avg_exposure * x for x in full_r]   # same average exposure as regime

    s_reg, s_const, s_full = _sharpe(regime_r), _sharpe(const_r), _sharpe(full_r)
    dd_reg, dd_const, dd_full = _max_drawdown(regime_r), _max_drawdown(const_r), _max_drawdown(full_r)

    beats = s_reg > s_const
    verdict = (
        f"OUT-OF-SAMPLE ({len(regime_r)} days): regime sizing Sharpe {s_reg:.2f} vs "
        f"fair constant-exposure baseline {s_const:.2f} "
        f"({'BEATS — regime timing added value' if beats else 'does NOT beat — the matrix is decoration here'}). "
        f"Max drawdown {dd_reg*100:.1f}% vs {dd_full*100:.1f}% at full exposure. "
        f"Avg regime exposure {avg_exposure:.2f}."
    )
    return {
        "ok": True, "test_days": len(regime_r), "avg_exposure": round(avg_exposure, 3),
        "sharpe_regime": round(s_reg, 3), "sharpe_const_avg": round(s_const, 3),
        "sharpe_full": round(s_full, 3),
        "maxdd_regime": round(dd_reg, 4), "maxdd_const_avg": round(dd_const, 4),
        "maxdd_full": round(dd_full, 4),
        "regime_beats_baseline": bool(beats), "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# shadow ledger (mirrors the copilot ledger; UTC; changes nothing)
# --------------------------------------------------------------------------- #
class RegimeLedger:
    def __init__(self, path=None):
        here = os.path.dirname(os.path.abspath(__file__))
        self.path = path or os.getenv(
            "KTRADE_REGIME_LEDGER", os.path.join(os.path.dirname(here), "logs",
                                                 "ktrade_regime_ledger.jsonl"))

    def record(self, snapshot, benchmark="SPY"):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            row = {"ts": datetime.now(timezone.utc).isoformat(), "benchmark": benchmark}
            row.update(snapshot or {})
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# loading + CLI
# --------------------------------------------------------------------------- #
def load_prices_csv(path):
    closes = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = "close" if "close" in row else ("Close" if "Close" in row else None)
            if key is None:
                continue
            try:
                closes.append(float(row[key]))
            except (TypeError, ValueError):
                continue
    return closes


def main(argv=None):
    ap = argparse.ArgumentParser(description="KTrade volatility-regime estimator (shadow).")
    ap.add_argument("--prices", required=True, help="CSV with a close column (SPY recommended)")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    prices = load_prices_csv(args.prices)
    est = RegimeEstimator(window=args.window)
    cur = est.current(prices) if len(prices) > args.window else {"regime": None}
    ev = evaluate_regime_value(prices, train_frac=args.train_frac, window=args.window)

    if args.json:
        print(json.dumps({"current": cur, "evaluation": ev}, indent=2))
        return 0

    print("=" * 64)
    print("KTrade Volatility-Regime Estimator (SHADOW — changes nothing)")
    print("=" * 64)
    print(f"Bars: {len(prices)}")
    print(f"Current regime         : {cur.get('regime')}  (vol {cur.get('realized_vol')})")
    print(f"  P(escalate to HIGH)  : {cur.get('escalation_prob')}")
    print(f"  suggested size mult  : {cur.get('suggested_size_mult')}  (suggestion only)")
    print("-" * 64)
    if ev.get("ok"):
        print(f"VERDICT: {ev['verdict']}")
        if not ev["regime_beats_baseline"]:
            print("=> Do NOT wire this into sizing. It did not beat the dumb baseline here.")
        else:
            print("=> Promising on THIS data. Re-test on other periods (incl. 2020) before trusting it.")
    else:
        print(f"Evaluation unavailable: {ev.get('reason')}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
