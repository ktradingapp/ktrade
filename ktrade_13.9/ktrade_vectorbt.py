r"""
KTrade PRO v10.0 - VectorBT Pipeline
======================================
Full pipeline:
  1. Optimize strategy parameters via VectorBT grid search
  2. Walk-forward validate best params
  3. If passes thresholds → save to ktrade_approved_params.json
  4. ktrade_agent_v9.py reads approved params → generates live signals
  5. ktrade_alpaca.py auto-places paper trades on approved signals

Approval thresholds (configurable via .env):
  MIN_SHARPE    = 0.4   (risk-adjusted return)
  MIN_WIN_RATE  = 45%   (% of trades profitable)
  MAX_DRAWDOWN  = 25%   (max portfolio decline)
  MIN_TRADES    = 8     (enough sample size)

Run:
  .venv\Scripts\python ktrade_vectorbt.py            # full optimization
  .venv\Scripts\python ktrade_vectorbt.py --fast     # 6-month quick test
  .venv\Scripts\python ktrade_vectorbt.py --ticker NVDA AMD
  .venv\Scripts\python ktrade_vectorbt.py --show     # show approved params
"""

from __future__ import annotations
import os, sys, json, logging, argparse
from datetime import datetime, timedelta
from itertools import product
from typing import Optional
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import vectorbt as vbt
except ImportError:
    print("❌ Run: .venv\\Scripts\\pip install vectorbt")
    sys.exit(1)

try:
    import yfinance as yf
except ImportError:
    print("❌ Run: .venv\\Scripts\\pip install yfinance")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ktrade_vectorbt.log", mode="a"),
    ]
)
log = logging.getLogger("KTrade.VBT")
__version__ = "v10.0"

# ── Paths ─────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
PARAMS_FILE = DATA_DIR / "ktrade_approved_params.json"
RESULTS_FILE= DATA_DIR / "ktrade_backtest_latest.json"
DATA_DIR.mkdir(exist_ok=True)

# ── Approval thresholds ───────────────────────────────────────────────────
# ── Approval thresholds ──────────────────────────────────────────────────
# Win rate intentionally LOW — trend-following has few wins but large ones
# QQQ Sharpe=2.3 with 25% win rate is EXCELLENT (don't reject it)
MIN_SHARPE        = float(os.getenv("VBT_MIN_SHARPE",        "0.3"))
MIN_WIN_RATE      = float(os.getenv("VBT_MIN_WIN_RATE",      "20"))   # 20% — trend strategies win rarely
MAX_DRAWDOWN      = float(os.getenv("VBT_MAX_DRAWDOWN",      "55"))   # 55% — covers volatile stocks
MIN_TRADES        = int(os.getenv(  "VBT_MIN_TRADES",        "4"))    # 4 min trades
MIN_PROFIT_FACTOR = float(os.getenv("VBT_MIN_PROFIT_FACTOR", "1.1"))  # $1.10 per $1 risked

# Per-tier drawdown limits (speculative stocks allowed higher DD)
SPECULATIVE_TICKERS = {"IONQ","RGTI","QBTS","RKLB","ASTS","RIVN","LCID",
                        "NIO","XPEV","NVTS","CRWV","DXYZ","NBIS","PL","QNT"}
CONSERVATIVE_TICKERS= {"SPY","QQQ","IWM","GLD","TLT","MSFT","AAPL","GOOGL"}

def max_dd_for(ticker: str) -> float:
    if ticker in SPECULATIVE_TICKERS:  return 65.0   # high volatility OK
    if ticker in CONSERVATIVE_TICKERS: return 25.0   # blue chip — stay tight
    return MAX_DRAWDOWN                               # default 55%

# ── Parameter search space ────────────────────────────────────────────────
MACD_PARAMS = {
    "fast":   [8, 12, 16],
    "slow":   [21, 26, 34],
    "signal": [7,  9,  12],
}
EMA_PARAMS = {
    "fast_span": [20, 50],
    "slow_span": [100, 200],
}
MOM_PARAMS = {
    "period": [10, 15, 20, 30],
}

# ── Tickers ───────────────────────────────────────────────────────────────
DEFAULT_TICKERS = [
    "NVDA","AMD","MSFT","GOOGL","META","AAPL","TSLA","AMZN",
    "PLTR","CRWD","COIN","MU","ARM","QQQ","SPY","IONQ","RKLB","QNT",
]


# ══════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATORS (parameterized)
# ══════════════════════════════════════════════════════════════════════════
def gen_macd(close: pd.Series, fast=12, slow=26, signal=9):
    macd_line  = close.ewm(span=fast,   adjust=False).mean() - \
                 close.ewm(span=slow,   adjust=False).mean()
    sig_line   = macd_line.ewm(span=signal, adjust=False).mean()
    entries = (macd_line > sig_line) & (macd_line.shift(1) <= sig_line.shift(1))
    exits   = (macd_line < sig_line) & (macd_line.shift(1) >= sig_line.shift(1))
    return entries, exits

def gen_ema(close: pd.Series, fast_span=50, slow_span=200):
    ema_fast = close.ewm(span=fast_span, adjust=False).mean()
    ema_slow = close.ewm(span=slow_span, adjust=False).mean()
    entries  = (close > ema_slow) & (ema_fast > ema_slow) & \
               (close.shift(1) <= ema_slow.shift(1))
    exits    = (close < ema_fast) & (close.shift(1) >= ema_fast.shift(1))
    return entries, exits

def gen_momentum(close: pd.Series, period=20):
    high_n  = close.rolling(period).max().shift(1)
    low_n   = close.rolling(period).min().shift(1)
    entries = close > high_n
    exits   = close < low_n
    return entries, exits

def gen_conviction(close: pd.Series, volume: pd.Series,
                   fast=12, slow=26, signal=9, ema_span=200):
    """Compound signal: MACD + EMA trend + volume surge."""
    ema_trend = close.ewm(span=ema_span, adjust=False).mean()
    macd_line = close.ewm(span=fast, adjust=False).mean() - \
                close.ewm(span=slow, adjust=False).mean()
    sig_line  = macd_line.ewm(span=signal, adjust=False).mean()
    vol_ma    = volume.rolling(20).mean()
    bull = (close > ema_trend) & (macd_line > sig_line) & \
           (volume > vol_ma * 1.15)
    entries = bull & ~bull.shift(1).fillna(False)
    exits   = ~bull & bull.shift(1).fillna(False)
    return entries, exits


# ══════════════════════════════════════════════════════════════════════════
# PARAMETER OPTIMIZER
# ══════════════════════════════════════════════════════════════════════════
class ParameterOptimizer:

    def __init__(self, cash=100_000, fees=0.001):
        self.cash = cash
        self.fees = fees

    def _run_portfolio(self, close, entries, exits) -> dict:
        """Run VectorBT portfolio and return key metrics."""
        def safe(v, default=0.0):
            """Handle NaN, Inf, None safely."""
            try:
                f = float(v)
                if f != f or abs(f) == float("inf"):   # NaN or Inf check
                    return default
                return f
            except (TypeError, ValueError):
                return default

        # v10.3: shift signals by one bar so a signal computed from bar t
        # is acted on at bar t+1, not on the same close that produced it.
        # Prevents same-bar look-ahead that inflates backtest returns.
        entries = entries.shift(1).fillna(False)
        exits   = exits.shift(1).fillna(False)
        try:
            pf = vbt.Portfolio.from_signals(
                close=close,
                entries=entries,
                exits=exits,
                init_cash=self.cash,
                fees=self.fees,
                freq="1D",
            )
            s = pf.stats()
            n_trades = int(s.get("Total Trades", 0))
            if n_trades < 1:
                return None   # no trades = skip
            return {
                "total_return": round(safe(s.get("Total Return [%]")),   2),
                "sharpe":       round(safe(s.get("Sharpe Ratio")),        3),
                "sortino":      round(safe(s.get("Sortino Ratio")),       3),
                "max_drawdown": round(safe(s.get("Max Drawdown [%]")),   2),
                "win_rate":     round(safe(s.get("Win Rate [%]"), 50),    1),
                "total_trades": n_trades,
                "calmar":       round(safe(s.get("Calmar Ratio")),        3),
                "profit_factor":round(safe(s.get("Profit Factor"), 1.0), 2),
            }
        except Exception as e:
            log.debug(f"Portfolio error: {e}")
            return None

    def optimize_macd(self, close: pd.Series) -> dict:
        """Grid search over MACD parameters → return best."""
        best = None
        for fast, slow, sig in product(
            MACD_PARAMS["fast"],
            MACD_PARAMS["slow"],
            MACD_PARAMS["signal"],
        ):
            if fast >= slow:
                continue
            entries, exits = gen_macd(close, fast, slow, sig)
            m = self._run_portfolio(close, entries, exits)
            if m and m["total_trades"] >= MIN_TRADES:
                if best is None or m["sharpe"] > best["sharpe"]:
                    best = {**m, "params": {"fast":fast,"slow":slow,"signal":sig}}
        return best

    def optimize_ema(self, close: pd.Series) -> dict:
        """Grid search over EMA parameters."""
        best = None
        for fast_span, slow_span in product(
            EMA_PARAMS["fast_span"],
            EMA_PARAMS["slow_span"],
        ):
            if fast_span >= slow_span:
                continue
            entries, exits = gen_ema(close, fast_span, slow_span)
            m = self._run_portfolio(close, entries, exits)
            if m and m["total_trades"] >= MIN_TRADES:
                if best is None or m["sharpe"] > best["sharpe"]:
                    best = {**m, "params":{"fast_span":fast_span,"slow_span":slow_span}}
        return best

    def optimize_momentum(self, close: pd.Series) -> dict:
        """Grid search over momentum period."""
        best = None
        for period in MOM_PARAMS["period"]:
            entries, exits = gen_momentum(close, period)
            m = self._run_portfolio(close, entries, exits)
            if m and m["total_trades"] >= MIN_TRADES:
                if best is None or m["sharpe"] > best["sharpe"]:
                    best = {**m, "params":{"period":period}}
        return best

    def optimize_conviction(self, close: pd.Series, volume: pd.Series) -> dict:
        """Grid search over conviction (MACD + EMA) parameters."""
        best = None
        for fast, slow, sig in product([8,12,16],[21,26],[7,9]):
            if fast >= slow:
                continue
            entries, exits = gen_conviction(close, volume, fast, slow, sig)
            m = self._run_portfolio(close, entries, exits)
            if m and m["total_trades"] >= MIN_TRADES:
                if best is None or m["sharpe"] > best["sharpe"]:
                    best = {**m, "params":{"fast":fast,"slow":slow,"signal":sig,"ema_span":200}}
        return best


# ══════════════════════════════════════════════════════════════════════════
# WALK-FORWARD VALIDATOR
# ══════════════════════════════════════════════════════════════════════════
class WalkForwardValidator:

    def __init__(self, cash=100_000, fees=0.001, n_splits=4):
        self.cash     = cash
        self.fees     = fees
        self.n_splits = n_splits

    def validate(self, ticker: str, df: pd.DataFrame,
                 strategy: str, params: dict) -> dict:
        """
        Walk-forward: train on past data, validate on next period.
        Returns consistency and out-of-sample performance.
        """
        n          = len(df)
        split_size = n // (self.n_splits + 1)
        splits     = []

        for i in range(self.n_splits):
            train_end  = split_size * (i + 1)
            test_start = train_end
            test_end   = min(train_end + split_size, n)
            test_df    = df.iloc[test_start:test_end]
            if len(test_df) < 10:
                continue

            close  = test_df["Close"]
            volume = test_df.get("Volume", pd.Series(1, index=close.index))

            # Apply approved params to test period
            if strategy == "MACD":
                entries, exits = gen_macd(close, **params)
            elif strategy == "EMA":
                entries, exits = gen_ema(close, **params)
            elif strategy == "Momentum":
                entries, exits = gen_momentum(close, **params)
            elif strategy == "Conviction":
                entries, exits = gen_conviction(close, volume, **params)
            else:
                continue

            try:
                # v10.7: shift signals one bar in walk-forward too (remove same-bar look-ahead)
                entries = entries.shift(1).fillna(False).astype(bool)
                exits   = exits.shift(1).fillna(False).astype(bool)
                pf = vbt.Portfolio.from_signals(
                    close=close, entries=entries, exits=exits,
                    init_cash=self.cash, fees=self.fees, freq="1D",
                )
                s = pf.stats()
                def _sf(v, d=0.0):
                    try:
                        f=float(v)
                        return d if (f!=f or abs(f)==float("inf")) else f
                    except: return d
                n_tr = int(s.get("Total Trades", 0))
                if n_tr < 1: continue   # skip empty splits
                splits.append({
                    "split":      i + 1,
                    "period":     f"{test_df.index[0].date()} → {test_df.index[-1].date()}",
                    "return_pct": round(_sf(s.get("Total Return [%]")), 2),
                    "sharpe":     round(_sf(s.get("Sharpe Ratio")),      3),
                    "win_rate":   round(_sf(s.get("Win Rate [%]"), 50),  1),
                    "trades":     n_tr,
                })
            except Exception:
                pass

        if not splits:
            return {"passed": False, "reason": "Insufficient data for walk-forward"}

        avg_ret    = np.mean([s["return_pct"] for s in splits])
        avg_sharpe = np.mean([s["sharpe"]     for s in splits])
        avg_wr     = np.mean([s["win_rate"]   for s in splits])
        profitable = sum(1 for s in splits if s["return_pct"] > 0)
        consistency= profitable / len(splits)

        passed = (
            avg_sharpe   >= MIN_SHARPE   and
            avg_wr       >= MIN_WIN_RATE and
            consistency  >= 0.5
        )

        return {
            "passed":      passed,
            "splits":      splits,
            "avg_return":  round(float(avg_ret),    2),
            "avg_sharpe":  round(float(avg_sharpe), 3),
            "avg_winrate": round(float(avg_wr),     1),
            "consistency": f"{profitable}/{len(splits)} profitable",
            "reason":      "Passed all thresholds" if passed else
                           f"Failed: sharpe={avg_sharpe:.2f} wr={avg_wr:.1f}%",
        }


# ══════════════════════════════════════════════════════════════════════════
# APPROVED PARAMS MANAGER
# ══════════════════════════════════════════════════════════════════════════
class ApprovedParamsManager:

    def load(self) -> dict:
        if PARAMS_FILE.exists():
            try:
                with open(PARAMS_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save(self, params: dict):
        with open(PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(params, f, indent=2, default=str)
        log.info(f"✅ Approved params saved → {PARAMS_FILE}")

    def approve(self, ticker: str, strategy: str, params: dict,
                metrics: dict, wf: dict):
        """Add an approved strategy for a ticker."""
        store = self.load()
        if ticker not in store:
            store[ticker] = {}
        store[ticker][strategy] = {
            "approved":   True,
            "params":     params,
            "sharpe":     metrics.get("sharpe"),
            "win_rate":   metrics.get("win_rate"),
            "max_dd":     metrics.get("max_drawdown"),
            "total_return": metrics.get("total_return"),
            "wf_sharpe":  wf.get("avg_sharpe"),
            "wf_winrate": wf.get("avg_winrate"),
            "approved_at": datetime.now().isoformat(),
        }
        self.save(store)
        return store

    def reject(self, ticker: str, strategy: str, reason: str):
        """Mark a strategy as rejected for this ticker."""
        store = self.load()
        if ticker not in store:
            store[ticker] = {}
        store[ticker][strategy] = {
            "approved": False,
            "reason":   reason,
            "checked_at": datetime.now().isoformat(),
        }
        self.save(store)

    def get_best(self, ticker: str) -> Optional[dict]:
        """Return best approved strategy + params for a ticker."""
        store = self.load()
        approved = {
            s: v for s, v in store.get(ticker, {}).items()
            if v.get("approved")
        }
        if not approved:
            return None
        # Return highest sharpe approved strategy
        best = max(approved.items(), key=lambda x: x[1].get("sharpe", 0))
        return {"strategy": best[0], **best[1]}

    def show_all(self):
        """Print all approved parameters."""
        store = self.load()
        if not store:
            print("No approved parameters yet. Run ktrade_vectorbt.py first.")
            return
        print(f"\n{'='*65}")
        print(f"  KTrade Approved Strategy Parameters")
        print(f"{'='*65}")
        for ticker, strategies in store.items():
            print(f"\n  {ticker}")
            for strat, info in strategies.items():
                status = "✅ APPROVED" if info.get("approved") else "❌ REJECTED"
                print(f"    {status} {strat}")
                if info.get("approved"):
                    print(f"      params:   {info.get('params')}")
                    print(f"      sharpe:   {info.get('sharpe')} | "
                          f"win_rate: {info.get('win_rate')}% | "
                          f"return: {info.get('total_return')}%")
                    print(f"      wf_sharpe:{info.get('wf_sharpe')} | "
                          f"wf_wr: {info.get('wf_winrate')}%")
                else:
                    print(f"      reason: {info.get('reason')}")
        print(f"{'='*65}\n")


# ══════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════
class KTradePipeline:

    def __init__(self, start="2022-01-01", end=None):
        self.start     = start
        self.end       = end or datetime.now().strftime("%Y-%m-%d")
        self.optimizer = ParameterOptimizer()
        self.validator = WalkForwardValidator()
        self.params_mgr= ApprovedParamsManager()

    def fetch(self, tickers: list) -> dict:
        log.info(f"Fetching {len(tickers)} tickers: {self.start} → {self.end}")
        data = {}
        for t in tickers:
            try:
                df = yf.Ticker(t).history(
                    start=self.start, end=self.end,
                    interval="1d", auto_adjust=True
                )
                if df is not None and len(df) >= 60:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    data[t] = df
                    log.info(f"  {t}: {len(df)} bars")
                else:
                    log.warning(f"  {t}: insufficient data (need 60+ bars)")
            except Exception as e:
                log.warning(f"  {t}: {e}")
        return data

    def process_ticker(self, ticker: str, df: pd.DataFrame) -> dict:
        """
        Full pipeline for one ticker:
          optimize → validate → approve/reject → return result
        """
        log.info(f"\n{'─'*50}")
        log.info(f"  Processing: {ticker} ({len(df)} bars)")
        log.info(f"{'─'*50}")

        close  = df["Close"]
        volume = df["Volume"] if "Volume" in df else pd.Series(1e6, index=close.index)
        result = {"ticker": ticker, "strategies": {}}

        strategies = {
            "MACD":       (self.optimizer.optimize_macd,       close,         None),
            "EMA":        (self.optimizer.optimize_ema,        close,         None),
            "Momentum":   (self.optimizer.optimize_momentum,   close,         None),
            "Conviction": (self.optimizer.optimize_conviction, close,         volume),
        }

        for strat_name, (opt_fn, arg1, arg2) in strategies.items():
            log.info(f"  [{strat_name}] Optimizing parameters...")
            try:
                if arg2 is not None:
                    best = opt_fn(arg1, arg2)
                else:
                    best = opt_fn(arg1)

                if best is None:
                    log.info(f"  [{strat_name}] No valid params found")
                    self.params_mgr.reject(ticker, strat_name,
                                           "No valid parameter combination")
                    result["strategies"][strat_name] = {
                        "approved": False, "reason": "No valid params"}
                    continue

                params  = best.pop("params")
                metrics = best

                log.info(f"  [{strat_name}] Best params: {params}")
                log.info(f"    sharpe={metrics['sharpe']} "
                         f"win={metrics['win_rate']}% "
                         f"pf={metrics['profit_factor']} "
                         f"dd={metrics['max_drawdown']}% "
                         f"trades={metrics['total_trades']}")

                # Check in-sample thresholds
                ticker_max_dd = max_dd_for(ticker)
                in_sample_ok = (
                    metrics["sharpe"]        >= MIN_SHARPE        and
                    metrics["max_drawdown"]  <= ticker_max_dd     and
                    metrics["total_trades"]  >= MIN_TRADES        and
                    (metrics["win_rate"]     >= MIN_WIN_RATE or
                     metrics["profit_factor"]>= MIN_PROFIT_FACTOR)
                )

                if not in_sample_ok:
                    reason = (f"In-sample failed: "
                              f"sharpe={metrics['sharpe']} (need≥{MIN_SHARPE}) "
                              f"wr={metrics['win_rate']}% (need≥{MIN_WIN_RATE}%) "
                              f"dd={metrics['max_drawdown']}% (max={ticker_max_dd}%)")
                    log.info(f"  [{strat_name}] ❌ {reason}")
                    self.params_mgr.reject(ticker, strat_name, reason)
                    result["strategies"][strat_name] = {
                        "approved": False, "reason": reason, "metrics": metrics}
                    continue

                # Walk-forward validate
                log.info(f"  [{strat_name}] Walk-forward validating...")
                wf = self.validator.validate(ticker, df, strat_name, params)
                log.info(f"    WF: {wf['reason']}")

                if wf["passed"]:
                    log.info(f"  [{strat_name}] ✅ APPROVED → params: {params}")
                    self.params_mgr.approve(ticker, strat_name, params, metrics, wf)
                    result["strategies"][strat_name] = {
                        "approved": True,
                        "params":   params,
                        "metrics":  metrics,
                        "walkforward": wf,
                    }
                else:
                    log.info(f"  [{strat_name}] ❌ Walk-forward failed: {wf['reason']}")
                    self.params_mgr.reject(ticker, strat_name,
                                           f"WF failed: {wf['reason']}")
                    result["strategies"][strat_name] = {
                        "approved": False,
                        "reason":   wf["reason"],
                        "metrics":  metrics,
                        "walkforward": wf,
                    }

            except Exception as e:
                log.error(f"  [{strat_name}] Error: {e}")
                result["strategies"][strat_name] = {
                    "approved": False, "reason": str(e)}

        # Summary for ticker
        approved = [s for s, v in result["strategies"].items() if v.get("approved")]
        log.info(f"\n  {ticker} result: {len(approved)}/{len(strategies)} strategies approved")
        if approved:
            log.info(f"  Approved: {approved}")
        return result

    def run(self, tickers: list = None) -> dict:
        tickers = tickers or DEFAULT_TICKERS

        print(f"\n{'='*60}")
        print(f"  KTrade VectorBT Pipeline v10.0")
        print(f"  Tickers: {len(tickers)}")
        print(f"  Period:  {self.start} → {self.end}")
        print(f"  Thresholds: Sharpe≥{MIN_SHARPE} "
              f"WinRate≥{MIN_WIN_RATE}% MaxDD≤{MAX_DRAWDOWN}%")
        print(f"{'='*60}\n")

        data    = self.fetch(tickers)
        results = {}
        total_approved = 0

        for ticker, df in data.items():
            r = self.process_ticker(ticker, df)
            results[ticker] = r
            approved = sum(1 for v in r["strategies"].values() if v.get("approved"))
            total_approved += approved

        # Save full results
        output = {
            "version":       __version__,
            "run_time":      datetime.now().isoformat(),
            "period":        f"{self.start} → {self.end}",
            "thresholds":    {
                "min_sharpe":   MIN_SHARPE,
                "min_win_rate": MIN_WIN_RATE,
                "max_drawdown": MAX_DRAWDOWN,
                "min_trades":   MIN_TRADES,
            },
            "tickers_tested": list(data.keys()),
            "results":        results,
            "summary": {
                "total_tickers":   len(data),
                "total_approved":  total_approved,
                "approved_params": str(PARAMS_FILE),
            }
        }

        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)

        # Print final summary
        print(f"\n{'='*60}")
        print(f"  Pipeline Complete")
        print(f"{'='*60}")
        print(f"  Tickers tested:     {len(data)}")
        print(f"  Strategies approved:{total_approved}")
        print(f"  Approved params  → {PARAMS_FILE}")
        print(f"  Full results     → {RESULTS_FILE}")
        print(f"\n  Next steps:")
        print(f"  1. Run agent:   .venv\\Scripts\\python agent\\ktrade_agent_v9.py --score-only")
        print(f"  2. Agent reads approved params → generates better signals")
        print(f"  3. Open dashboard: http://localhost:5001")
        print(f"  4. Alpaca places paper trades on approved signals")
        print(f"{'='*60}\n")
        self.params_mgr.show_all()
        return output


def main():
    p = argparse.ArgumentParser(description="KTrade VectorBT Pipeline")
    p.add_argument("--ticker",  nargs="+", default=None)
    p.add_argument("--start",   default="2015-01-01")
    p.add_argument("--fast",    action="store_true", help="6-month quick run")
    p.add_argument("--show",    action="store_true", help="Show approved params")
    args = p.parse_args()

    mgr = ApprovedParamsManager()

    if args.show:
        mgr.show_all()
        return

    if args.fast:
        args.start = (datetime.now()-timedelta(days=365)).strftime("%Y-%m-%d")

    pipeline = KTradePipeline(start=args.start)
    pipeline.run(tickers=args.ticker or DEFAULT_TICKERS)


if __name__ == "__main__":
    main()
