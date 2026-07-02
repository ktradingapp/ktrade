#!/usr/bin/env python3
"""KTrade parameter-fragility sweep (v13.6).

The problem this solves: KTrade's optimizer (ktrade_vectorbt.py) grid-searches
parameters and keeps the SINGLE best Sharpe. That is exactly how you get a
curve-fit strategy — a sharp peak at one lucky parameter set that collapses the
moment the market shifts. A *robust* edge instead shows a broad PLATEAU: many
nearby parameter sets all perform reasonably. This module sweeps a parameter grid
and tells you which one you have.

It reports, over the whole grid (not just the winner):
  - profitable_fraction : share of parameter sets that are profitable at all;
  - plateau_ratio       : how much of the best result its immediate neighbours
                          retain (high = plateau, low = lonely spike);
  - cliff_fraction      : share of adjacent parameter pairs that flip
                          profitable<->unprofitable (high = brittle surface);
  - a classification    : ROBUST / MIXED / FRAGILE, leaning conservative.

Two layers:
  * the ENGINE (sweep + analyze_fragility) is pure and dependency-light — it takes
    an injectable backtest_fn(params)->metrics, so it's fully unit-tested offline.
  * a VectorBT ADAPTER wires it to KTrade's real gen_* strategies + portfolio math.
    That path needs vectorbt + price data, so it runs on your VPS, not in CI.

  python agent/param_fragility.py --prices NVDA.csv --strategy macd
        # CSV columns: date,close[,volume]
"""
import argparse
import csv
import itertools
import json
import os
import random
import statistics


# --------------------------------------------------------------------------- #
# engine (pure; higher metric = better, e.g. sharpe / total_return)
# --------------------------------------------------------------------------- #
def sweep(backtest_fn, param_grid, metric="sharpe"):
    """Run backtest_fn over every combination in param_grid.
    Returns [{params, value, metrics}, ...]; value is metrics[metric] or None."""
    names = list(param_grid.keys())
    grids = [list(param_grid[n]) for n in names]
    results = []
    for combo in itertools.product(*grids):
        params = dict(zip(names, combo))
        try:
            m = backtest_fn(params)
        except Exception:
            m = None
        value = m[metric] if (isinstance(m, dict) and metric in m and m[metric] is not None) else None
        results.append({"params": params, "value": value, "metrics": m})
    return results


def analyze_fragility(results, param_grid, metric="sharpe", profit_threshold=0.0):
    names = list(param_grid.keys())
    grids = [list(param_grid[n]) for n in names]
    idx_of = [{v: i for i, v in enumerate(g)} for g in grids]

    vmap = {}
    for r in results:
        if r.get("value") is None:
            continue
        try:
            it = tuple(idx_of[d][r["params"][names[d]]] for d in range(len(names)))
        except (KeyError, IndexError):
            continue
        vmap[it] = float(r["value"])

    values = list(vmap.values())
    if not values:
        return {"ok": False, "reason": "no valid backtest results (every combo errored or made no trades)"}

    best = max(values)
    n_valued = len(values)
    profitable = sum(1 for v in values if v > profit_threshold)
    profitable_fraction = profitable / n_valued

    best_it = max(vmap, key=lambda k: vmap[k])
    best_params = {names[d]: grids[d][best_it[d]] for d in range(len(names))}

    neigh = []
    for d in range(len(names)):
        for step in (-1, 1):
            j = best_it[d] + step
            if 0 <= j < len(grids[d]):
                ni = best_it[:d] + (j,) + best_it[d + 1:]
                if ni in vmap:
                    neigh.append(vmap[ni])
    neighbor_mean = statistics.mean(neigh) if neigh else None
    plateau_ratio = (neighbor_mean / best) if (neighbor_mean is not None and best > 0) else None

    pairs = flips = 0
    for it, v in vmap.items():
        for d in range(len(names)):
            ni = it[:d] + (it[d] + 1,) + it[d + 1:]
            if ni in vmap:
                pairs += 1
                if (v > profit_threshold) != (vmap[ni] > profit_threshold):
                    flips += 1
    cliff_fraction = (flips / pairs) if pairs else None

    mean = statistics.mean(values)
    cv = (statistics.pstdev(values) / abs(mean)) if (len(values) > 1 and mean) else None

    robust = (profitable_fraction >= 0.5 and plateau_ratio is not None
              and plateau_ratio >= 0.5 and (cliff_fraction is None or cliff_fraction <= 0.3))
    fragile = (profitable_fraction < 0.3 or (plateau_ratio is not None and plateau_ratio < 0.2)
               or (cliff_fraction is not None and cliff_fraction > 0.5))
    classification = "ROBUST" if robust else ("FRAGILE" if fragile else "MIXED")

    return {
        "ok": True, "metric": metric, "n_combos": len(results), "n_valued": n_valued,
        "best": round(best, 3), "best_params": best_params,
        "mean": round(mean, 3), "worst": round(min(values), 3),
        "profitable_fraction": round(profitable_fraction, 3),
        "neighbor_mean": round(neighbor_mean, 3) if neighbor_mean is not None else None,
        "plateau_ratio": round(plateau_ratio, 3) if plateau_ratio is not None else None,
        "cliff_fraction": round(cliff_fraction, 3) if cliff_fraction is not None else None,
        "coeff_variation": round(cv, 3) if cv is not None else None,
        "classification": classification, "robust": robust,
        "verdict": _verdict(classification, best_params, metric, profitable_fraction,
                            plateau_ratio, cliff_fraction),
    }


def _verdict(cls, best_params, metric, prof_frac, plateau, cliff):
    head = {
        "ROBUST": "ROBUST — the edge holds across a plateau of nearby parameters, not just the winner.",
        "FRAGILE": "FRAGILE — performance is a lonely spike; this looks CURVE-FIT, not a real edge.",
        "MIXED": "MIXED — partially robust; treat with caution and re-test out-of-sample.",
    }[cls]
    return (f"{head} best={best_params} | profitable {prof_frac*100:.0f}% of grid, "
            f"plateau_ratio={plateau}, cliff_fraction={cliff} (on {metric}). "
            f"A robust edge wants profitable% high, plateau_ratio >= ~0.5, cliff_fraction low.")


def format_report(analysis, param_grid, metric="sharpe"):
    if not analysis.get("ok"):
        return f"Fragility analysis unavailable: {analysis.get('reason')}"
    a = analysis
    lines = ["=" * 64, f"KTrade Parameter-Fragility Sweep ({metric})", "=" * 64]
    lines.append(f"Grid: " + ", ".join(f"{k}={list(v)}" for k, v in param_grid.items()))
    lines.append(f"Combos: {a['n_combos']} ({a['n_valued']} produced trades)")
    lines.append(f"Best {metric}: {a['best']}  at {a['best_params']}")
    lines.append(f"Grid mean / worst: {a['mean']} / {a['worst']}")
    lines.append("-" * 64)
    lines.append(f"Profitable fraction : {a['profitable_fraction']}  (share of grid that's profitable)")
    lines.append(f"Plateau ratio       : {a['plateau_ratio']}  (neighbours / best — high = plateau)")
    lines.append(f"Cliff fraction      : {a['cliff_fraction']}  (adjacent sign-flips — low = stable)")
    lines.append(f"Coeff of variation  : {a['coeff_variation']}")
    lines.append("-" * 64)
    lines.append(f"CLASSIFICATION: {a['classification']}")
    lines.append("VERDICT: " + a["verdict"])
    lines.append("=" * 64)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# default grids around KTrade's existing approved params
# --------------------------------------------------------------------------- #
DEFAULT_GRIDS = {
    "macd": {"fast": [8, 10, 12], "slow": [21, 26, 30], "signal": [7, 9]},
    "ema": {"fast_span": [30, 50, 70], "slow_span": [100, 150, 200]},
    "momentum": {"period": [10, 20, 30, 40, 50]},
}


# --------------------------------------------------------------------------- #
# Monte-Carlo robustness (v13.10): is the edge real, or luck of a few trades?
# NOTE ON A COMMON MISTAKE: shuffling trade ORDER does NOT change Sharpe — mean and
# std are order-invariant, so "shuffle order, check Sharpe>0" tests nothing. The
# correct tests are: (a) BOOTSTRAP (resample trades WITH replacement) for whether
# the positive Sharpe survives sampling variability, and (b) order-SHUFFLE for
# DRAWDOWN, which IS path-dependent. Both are done below. Operates on per-trade
# fractional returns (0.02 = +2%). Pure stdlib.
# --------------------------------------------------------------------------- #
def _sharpe(returns):
    vals = [float(r) for r in returns]
    if len(vals) < 2:
        return 0.0
    sd = statistics.stdev(vals)
    return (statistics.mean(vals) / sd) if sd > 0 else 0.0


def _max_drawdown(returns):
    """Max peak-to-trough drawdown of the equity curve built from returns. <= 0."""
    equity = peak = 1.0
    mdd = 0.0
    for r in returns:
        equity *= (1.0 + float(r))
        if equity > peak:
            peak = equity
        dd = (equity - peak) / peak if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    return mdd


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def monte_carlo_analysis(trade_returns, n_boot=1000, dd_shuffles=1000, seed=0):
    """Bootstrap Sharpe robustness + order-shuffle drawdown distribution."""
    returns = [float(r) for r in (trade_returns or [])]
    n = len(returns)
    if n < 5:
        return {"ok": False, "reason": f"need >= 5 trades for Monte-Carlo, got {n}"}
    rng = random.Random(seed)

    # (a) bootstrap Sharpe: resample WITH replacement -> is Sharpe reliably > 0?
    boot = []
    for _ in range(n_boot):
        sample = [returns[rng.randrange(n)] for _ in range(n)]
        boot.append(_sharpe(sample))
    boot.sort()
    prob_positive = sum(1 for s in boot if s > 0) / len(boot)
    sharpe_p5 = _percentile(boot, 5)
    sharpe_p50 = _percentile(boot, 50)

    # (b) order-shuffle: Sharpe is order-invariant, but DRAWDOWN is not.
    dds = []
    idx = list(range(n))
    for _ in range(dd_shuffles):
        rng.shuffle(idx)
        dds.append(_max_drawdown([returns[i] for i in idx]))
    dds.sort()  # most-negative first
    dd_median = _percentile(dds, 50)
    dd_worst_p5 = _percentile(dds, 5)   # worst 5% of orderings

    robust = prob_positive >= 0.95 and sharpe_p5 is not None and sharpe_p5 > 0
    fragile = prob_positive < 0.90
    classification = "ROBUST" if robust else ("FRAGILE" if fragile else "MIXED")
    return {
        "ok": True, "n_trades": n,
        "observed_sharpe": round(_sharpe(returns), 3),
        "observed_max_drawdown_pct": round(_max_drawdown(returns) * 100, 2),
        "prob_sharpe_positive": round(prob_positive, 3),
        "bootstrap_sharpe_p5": round(sharpe_p5, 3) if sharpe_p5 is not None else None,
        "bootstrap_sharpe_median": round(sharpe_p50, 3) if sharpe_p50 is not None else None,
        "shuffled_median_max_dd_pct": round(dd_median * 100, 2) if dd_median is not None else None,
        "shuffled_worst5pct_max_dd_pct": round(dd_worst_p5 * 100, 2) if dd_worst_p5 is not None else None,
        "n_boot": n_boot, "n_dd_shuffles": dd_shuffles,
        "classification": classification, "robust": robust,
        "verdict": _mc_verdict(classification, prob_positive, sharpe_p5, dd_worst_p5),
    }


def _mc_verdict(cls, prob_pos, sharpe_p5, dd_worst):
    head = {
        "ROBUST": "ROBUST — the positive Sharpe survives resampling; the edge is unlikely to be luck.",
        "FRAGILE": "FRAGILE — Sharpe is not reliably positive under resampling; likely noise / curve-fit.",
        "MIXED": "MIXED — positive but not decisively; gather more trades before trusting it.",
    }[cls]
    dd = f"{dd_worst * 100:.1f}%" if dd_worst is not None else "n/a"
    return (f"{head} P(Sharpe>0)={prob_pos:.0%}, 5th-pctile bootstrap Sharpe={sharpe_p5}. "
            f"Worst-5% max drawdown across trade orderings ~ {dd}.")


def format_mc_report(mc):
    if not mc.get("ok"):
        return f"Monte-Carlo unavailable: {mc.get('reason')}"
    lines = ["=" * 64, "KTrade Monte-Carlo Robustness", "=" * 64]
    lines.append(f"Trades: {mc['n_trades']} | observed Sharpe {mc['observed_sharpe']} | "
                 f"observed max DD {mc['observed_max_drawdown_pct']}%")
    lines.append("-" * 64)
    lines.append(f"P(Sharpe > 0) under bootstrap : {mc['prob_sharpe_positive']}  "
                 f"({mc['n_boot']} resamples)")
    lines.append(f"Bootstrap Sharpe 5th / median : {mc['bootstrap_sharpe_p5']} / {mc['bootstrap_sharpe_median']}")
    lines.append(f"Shuffled max-DD median / worst-5% : {mc['shuffled_median_max_dd_pct']}% / "
                 f"{mc['shuffled_worst5pct_max_dd_pct']}%  ({mc['n_dd_shuffles']} orderings)")
    lines.append("-" * 64)
    lines.append(f"CLASSIFICATION: {mc['classification']}")
    lines.append("VERDICT: " + mc["verdict"])
    lines.append("=" * 64)
    return "\n".join(lines)


def _load_returns(path):
    """Load per-trade fractional returns from a file (one per line / CSV column).
    Ignores non-numeric tokens (headers), so it's format-tolerant."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            for tok in line.replace(",", " ").split():
                try:
                    out.append(float(tok))
                except ValueError:
                    pass
    return out


# --------------------------------------------------------------------------- #
# VectorBT adapter (verified-by-construction; runs on the VPS, not in CI)
# --------------------------------------------------------------------------- #
def make_vectorbt_backtest_fn(close, volume, strategy):
    """Return backtest_fn(params)->metrics using KTrade's own gen_* strategies and
    portfolio math (with the one-bar shift that prevents look-ahead). Imports are
    deferred so this module loads even where vectorbt isn't installed."""
    try:
        try:
            from ktrade_vectorbt import (ParameterOptimizer, gen_macd, gen_ema,
                                         gen_momentum, gen_conviction)
        except Exception:
            import sys as _sys
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            from ktrade_vectorbt import (ParameterOptimizer, gen_macd, gen_ema,
                                         gen_momentum, gen_conviction)
    except Exception as exc:
        raise RuntimeError(f"vectorbt harness unavailable ({exc}). Run on your VPS "
                           f"with vectorbt installed (pip install -r requirements.txt).")

    opt = ParameterOptimizer()
    gens = {"macd": gen_macd, "ema": gen_ema, "momentum": gen_momentum, "conviction": gen_conviction}
    gen = gens.get(strategy)
    if gen is None:
        raise ValueError(f"unknown strategy '{strategy}' (choices: {list(gens)})")

    def backtest_fn(params):
        if strategy == "conviction":
            entries, exits = gen(close, volume, **params)
        else:
            entries, exits = gen(close, **params)
        return opt._run_portfolio(close, entries, exits)   # None if no trades

    return backtest_fn


def load_prices_csv(path):
    """Return (close, volume) pandas Series from a date,close[,volume] CSV."""
    import pandas as pd
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    ck = "close" if rows and "close" in rows[0] else "Close"
    vk = "volume" if rows and "volume" in rows[0] else ("Volume" if rows and "Volume" in rows[0] else None)
    close = pd.Series([float(r[ck]) for r in rows if r.get(ck)])
    volume = pd.Series([float(r[vk]) for r in rows if r.get(vk)]) if vk else pd.Series([0.0] * len(close))
    return close, volume


def main(argv=None):
    ap = argparse.ArgumentParser(description="KTrade fragility tools (parameter grid + Monte-Carlo).")
    ap.add_argument("--prices", help="CSV: date,close[,volume] (parameter-grid fragility)")
    ap.add_argument("--mc-returns", dest="mc_returns",
                    help="file of per-trade fractional returns, one per line (Monte-Carlo robustness)")
    ap.add_argument("--strategy", default="macd", choices=list(DEFAULT_GRIDS) + ["conviction"])
    ap.add_argument("--metric", default="sharpe")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.mc_returns:
        mc = monte_carlo_analysis(_load_returns(args.mc_returns))
        print(json.dumps(mc, indent=2) if args.json else format_mc_report(mc))
        return 0 if mc.get("ok") else 1

    if not args.prices:
        ap.error("provide --prices (parameter-grid fragility) or --mc-returns (Monte-Carlo)")

    grid = DEFAULT_GRIDS.get(args.strategy, DEFAULT_GRIDS["macd"])
    try:
        close, volume = load_prices_csv(args.prices)
        backtest_fn = make_vectorbt_backtest_fn(close, volume, args.strategy)
    except Exception as exc:
        print(f"Cannot run real backtest here: {exc}")
        return 1

    results = sweep(backtest_fn, grid, metric=args.metric)
    analysis = analyze_fragility(results, grid, metric=args.metric)
    print(json.dumps(analysis, indent=2) if args.json else format_report(analysis, grid, args.metric))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
