"""
KTrade PRO - Intraday VectorBT Pipeline
=======================================
Purpose:
  Backtest intraday entry timing without replacing the existing daily VectorBT file.

Strategies:
  1. ORB_VWAP: Opening range breakout confirmed by VWAP and volume.
  2. VWAP_RECLAIM: Price reclaims VWAP with short EMA confirmation.

Outputs:
  data/ktrade_intraday_backtest_latest.json
  data/ktrade_intraday_approved_params.json

Recommended runs:
  python ktrade_intraday_vectorbt.py --interval 5m --period 60d --ticker SPY QQQ NVDA AAPL MSFT
  python ktrade_intraday_vectorbt.py --fast
  python ktrade_intraday_vectorbt.py --show

Notes:
  - 5-minute candles are the recommended first intraday mode.
  - 1-minute candles are noisy and usually limited to shorter history.
  - This is for paper-testing and research, not financial advice.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    import vectorbt as vbt
except ImportError:
    print("Run: .venv\\Scripts\\pip install vectorbt")
    sys.exit(1)

try:
    import yfinance as yf
except ImportError:
    print("Run: .venv\\Scripts\\pip install yfinance")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
RESULTS_FILE = DATA_DIR / "ktrade_intraday_backtest_latest.json"
PARAMS_FILE = DATA_DIR / "ktrade_intraday_approved_params.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("ktrade_intraday_vectorbt.log", mode="a")],
)
log = logging.getLogger("KTrade.IntradayVBT")

CORE_TICKERS = ["SPY", "QQQ", "IWM", "GLD", "TLT", "NVDA", "MSFT", "AAPL", "QNT"]
EXTENDED_TICKERS = [
    "SPY", "QQQ", "IWM", "GLD", "TLT", "NVDA", "MSFT", "AAPL", "QNT",
    "CEG", "VST", "GEV", "ETN", "PWR", "VRT", "MOD", "MPWR", "NVTS", "TLN",
    "SOXX", "XAR", "IDGT", "QTUM", "DRAM", "MRVL", "MU", "RMBS", "LEU",
    "RGTI", "QBTS", "IONQ", "TQQQ", "NVAX", "AMD", "INTC", "CRDO", "PL",
    "NOK", "ARM", "NBIS", "QCOM", "MSTR", "SMCI", "IREN", "CRWV", "RKLB",
    "IRDM", "KTOS", "DXYZ", "BTC-USD", "ETH-USD",
]
DEFAULT_TICKERS = CORE_TICKERS

MIN_SHARPE = float(os.getenv("IVBT_MIN_SHARPE", "0.25"))
MIN_WIN_RATE = float(os.getenv("IVBT_MIN_WIN_RATE", "35"))
MAX_DRAWDOWN = float(os.getenv("IVBT_MAX_DRAWDOWN", "20"))
MIN_TRADES = int(os.getenv("IVBT_MIN_TRADES", "5"))
MIN_PROFIT_FACTOR = float(os.getenv("IVBT_MIN_PROFIT_FACTOR", "1.05"))


def safe_float(value, default=0.0):
    try:
        f = float(value)
        if np.isnan(f) or np.isinf(f):
            return default
        return f
    except Exception:
        return default


def clean_intraday(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("America/New_York").tz_localize(None)
    needed = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        return pd.DataFrame()
    return df[needed].dropna()


def add_session_columns(df: pd.DataFrame, opening_minutes: int) -> pd.DataFrame:
    out = df.copy()
    out["date"] = out.index.date
    out["bar_in_day"] = out.groupby("date").cumcount()

    typical = (out["High"] + out["Low"] + out["Close"]) / 3.0
    pv = typical * out["Volume"].replace(0, np.nan)
    out["vwap"] = pv.groupby(out["date"]).cumsum() / out["Volume"].replace(0, np.nan).groupby(out["date"]).cumsum()

    # Approximate number of bars in opening range from actual median interval.
    if len(out.index) > 2:
        minutes = max(1, int(pd.Series(out.index).diff().median().total_seconds() // 60))
    else:
        minutes = 5
    opening_bars = max(1, int(opening_minutes / minutes))

    opening = out[out["bar_in_day"] < opening_bars]
    or_high = opening.groupby("date")["High"].max()
    or_low = opening.groupby("date")["Low"].min()
    out["or_high"] = out["date"].map(or_high)
    out["or_low"] = out["date"].map(or_low)
    out["after_opening_range"] = out["bar_in_day"] >= opening_bars
    out["late_day"] = out.groupby("date")["bar_in_day"].transform("max") - out["bar_in_day"] <= 2
    out["vol_ma"] = out["Volume"].rolling(20).mean()
    out["ema_fast"] = out["Close"].ewm(span=9, adjust=False).mean()
    out["ema_slow"] = out["Close"].ewm(span=21, adjust=False).mean()
    return out


def gen_orb_vwap(df: pd.DataFrame, opening_minutes=30, volume_mult=1.1):
    x = add_session_columns(df, opening_minutes)
    close = x["Close"]
    breakout = (close > x["or_high"]) & (close.shift(1) <= x["or_high"].shift(1))
    entries = breakout & x["after_opening_range"] & (close > x["vwap"]) & (x["Volume"] > x["vol_ma"] * volume_mult)
    exits = (close < x["vwap"]) | x["late_day"]
    return entries.fillna(False), exits.fillna(False)


def gen_vwap_reclaim(df: pd.DataFrame, volume_mult=1.0):
    x = add_session_columns(df, 30)
    close = x["Close"]
    reclaim = (close > x["vwap"]) & (close.shift(1) <= x["vwap"].shift(1))
    trend = x["ema_fast"] > x["ema_slow"]
    entries = reclaim & trend & x["after_opening_range"] & (x["Volume"] > x["vol_ma"] * volume_mult)
    exits = (close < x["ema_fast"]) | x["late_day"]
    return entries.fillna(False), exits.fillna(False)



def gen_trend_continuation(df: pd.DataFrame, start_minutes=45, volume_mult=0.8):
    """Intraday trend-following: enter after early noise if price holds above VWAP and EMAs are bullish."""
    x = add_session_columns(df, start_minutes)
    close = x["Close"]
    trend = (close > x["vwap"]) & (x["ema_fast"] > x["ema_slow"])
    healthy_volume = x["Volume"] >= (x["vol_ma"] * volume_mult)
    previous_trend = trend.shift(1).fillna(False).astype(bool)
    entries = trend & healthy_volume & x["after_opening_range"] & ~previous_trend
    exits = (close < x["ema_fast"]) | (close < x["vwap"]) | x["late_day"]
    return entries.fillna(False), exits.fillna(False)


def gen_vwap_pullback(df: pd.DataFrame, start_minutes=30, pullback_pct=0.0015):
    """Buy a shallow pullback toward VWAP only when the intraday trend is still bullish."""
    x = add_session_columns(df, start_minutes)
    close = x["Close"]
    bullish = (close > x["vwap"]) & (x["ema_fast"] > x["ema_slow"])
    near_vwap = ((close - x["vwap"]) / x["vwap"]).between(0, pullback_pct)
    reclaiming = close > close.shift(1)
    entries = bullish & near_vwap & reclaiming & x["after_opening_range"]
    exits = (close < x["vwap"]) | (close < x["ema_slow"]) | x["late_day"]
    return entries.fillna(False), exits.fillna(False)


def _rsi(close: pd.Series, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def gen_rsi_reversal(df: pd.DataFrame, start_minutes=45, low=32, high=58):
    """Mean reversion for liquid tickers: buy intraday oversold reversal back toward VWAP."""
    x = add_session_columns(df, start_minutes)
    close = x["Close"]
    rsi = _rsi(close, 14)
    oversold_reclaim = (rsi > low) & (rsi.shift(1) <= low)
    entries = oversold_reclaim & (close < x["vwap"] * 1.003) & x["after_opening_range"]
    exits = (rsi > high) | (close > x["vwap"]) | x["late_day"]
    return entries.fillna(False), exits.fillna(False)


def gen_prev_bar_breakout(df: pd.DataFrame, lookback=6, volume_mult=1.0):
    """Short intraday breakout: enter when price clears recent bars with volume confirmation."""
    x = add_session_columns(df, 30)
    close = x["Close"]
    prior_high = x["High"].rolling(lookback).max().shift(1)
    entries = (close > prior_high) & (close > x["vwap"]) & (x["Volume"] > x["vol_ma"] * volume_mult) & x["after_opening_range"]
    exits = (close < x["ema_fast"]) | (close < prior_high.shift(1)) | x["late_day"]
    return entries.fillna(False), exits.fillna(False)

def run_portfolio(close: pd.Series, entries: pd.Series, exits: pd.Series, interval: str) -> Optional[dict]:
    # Prevent same-bar look-ahead: a signal calculated from bar t is executed on bar t+1.
    entries = entries.shift(1).fillna(False).astype(bool)
    exits = exits.shift(1).fillna(False).astype(bool)
    try:
        pf = vbt.Portfolio.from_signals(
            close=close,
            entries=entries,
            exits=exits,
            init_cash=100_000,
            fees=0.001,
            freq=interval,
        )
        s = pf.stats()
        trades = int(s.get("Total Trades", 0))
        if trades < 1:
            return None
        return {
            "total_return": round(safe_float(s.get("Total Return [%]")), 2),
            "sharpe": round(safe_float(s.get("Sharpe Ratio")), 3),
            "sortino": round(safe_float(s.get("Sortino Ratio")), 3),
            "max_drawdown": round(safe_float(s.get("Max Drawdown [%]")), 2),
            "win_rate": round(safe_float(s.get("Win Rate [%]")), 1),
            "total_trades": trades,
            "profit_factor": round(safe_float(s.get("Profit Factor"), 1.0), 2),
        }
    except Exception as exc:
        log.debug("Portfolio failed: %s", exc)
        return None


def passed(metrics: dict) -> bool:
    return (
        metrics["total_trades"] >= MIN_TRADES
        and metrics["max_drawdown"] <= MAX_DRAWDOWN
        and metrics["sharpe"] >= MIN_SHARPE
        and (metrics["win_rate"] >= MIN_WIN_RATE or metrics["profit_factor"] >= MIN_PROFIT_FACTOR)
    )


def fetch(ticker: str, interval: str, period: str) -> pd.DataFrame:
    log.info("Fetching %s interval=%s period=%s", ticker, interval, period)
    return clean_intraday(yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True, prepost=False))


def process_ticker(ticker: str, df: pd.DataFrame, interval: str) -> dict:
    result = {"ticker": ticker, "bars": len(df), "strategies": {}}
    if len(df) < 100:
        result["error"] = "insufficient intraday bars"
        return result

    candidates = []

    def add_candidate(name: str, params: dict, entries: pd.Series, exits: pd.Series):
        metrics = run_portfolio(df["Close"], entries, exits, interval)
        if metrics:
            candidates.append((name, params, metrics))

    for opening_minutes in [15, 30, 45, 60]:
        for volume_mult in [0.8, 1.0, 1.1, 1.25, 1.5]:
            entries, exits = gen_orb_vwap(df, opening_minutes, volume_mult)
            add_candidate("ORB_VWAP", {"opening_minutes": opening_minutes, "volume_mult": volume_mult}, entries, exits)

    for volume_mult in [0.7, 0.9, 1.0, 1.1, 1.25]:
        entries, exits = gen_vwap_reclaim(df, volume_mult)
        add_candidate("VWAP_RECLAIM", {"volume_mult": volume_mult}, entries, exits)

    for start_minutes in [30, 45, 60, 90]:
        for volume_mult in [0.5, 0.7, 0.9, 1.1]:
            entries, exits = gen_trend_continuation(df, start_minutes, volume_mult)
            add_candidate("TREND_CONTINUATION", {"start_minutes": start_minutes, "volume_mult": volume_mult}, entries, exits)

    for start_minutes in [30, 45, 60, 90]:
        for pullback_pct in [0.001, 0.0015, 0.0025, 0.004, 0.006]:
            entries, exits = gen_vwap_pullback(df, start_minutes, pullback_pct)
            add_candidate("VWAP_PULLBACK", {"start_minutes": start_minutes, "pullback_pct": pullback_pct}, entries, exits)

    for start_minutes in [30, 45, 60, 90]:
        for low in [25, 30, 35, 40]:
            for high in [55, 60, 65]:
                entries, exits = gen_rsi_reversal(df, start_minutes, low, high)
                add_candidate("RSI_REVERSAL", {"start_minutes": start_minutes, "low": low, "high": high}, entries, exits)

    for lookback in [3, 6, 9, 12]:
        for volume_mult in [0.7, 1.0, 1.25]:
            entries, exits = gen_prev_bar_breakout(df, lookback, volume_mult)
            add_candidate("PREV_BAR_BREAKOUT", {"lookback": lookback, "volume_mult": volume_mult}, entries, exits)

    approved = {}
    strategy_names = [
        "ORB_VWAP", "VWAP_RECLAIM", "TREND_CONTINUATION", "VWAP_PULLBACK",
        "RSI_REVERSAL", "PREV_BAR_BREAKOUT",
    ]
    for name in strategy_names:
        matches = [(params, m) for n, params, m in candidates if n == name]
        if not matches:
            result["strategies"][name] = {"approved": False, "reason": "no trades"}
            continue
        params, metrics = max(matches, key=lambda item: (item[1]["sharpe"], item[1]["profit_factor"], item[1]["total_return"]))
        ok = passed(metrics)
        result["strategies"][name] = {"approved": ok, "params": params, "metrics": metrics}
        if ok:
            approved[name] = {"approved": True, "params": params, **metrics, "approved_at": datetime.now().isoformat()}

    # Store the best candidate even when it fails. This helps you see the best historical setup,
    # while still keeping approval honest.
    if candidates:
        best_name, best_params, best_metrics = max(candidates, key=lambda item: (item[2]["sharpe"], item[2]["profit_factor"], item[2]["total_return"]))
        result["best_candidate"] = {
            "strategy": best_name,
            "approved": passed(best_metrics),
            "params": best_params,
            "metrics": best_metrics,
        }
    result["approved_count"] = len(approved)
    return result

def load_approved() -> dict:
    if PARAMS_FILE.exists():
        try:
            return json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_approved(results: dict, interval: str, period: str):
    store = load_approved()
    for ticker, res in results.items():
        for strategy, info in res.get("strategies", {}).items():
            if info.get("approved"):
                store.setdefault(ticker, {})[strategy] = {
                    "approved": True,
                    "interval": interval,
                    "period": period,
                    "params": info.get("params", {}),
                    **info.get("metrics", {}),
                    "approved_at": datetime.now().isoformat(),
                }
    PARAMS_FILE.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")
    return store


def show():
    store = load_approved()
    if not store:
        print("No approved intraday params yet. Run ktrade_intraday_vectorbt.py first.")
        return
    print(json.dumps(store, indent=2))


def main():
    parser = argparse.ArgumentParser(description="KTrade Intraday VectorBT")
    parser.add_argument("--ticker", nargs="+", default=None)
    parser.add_argument("--interval", default="5m", choices=["1m", "5m", "15m"])
    parser.add_argument("--period", default="60d")
    parser.add_argument("--fast", action="store_true", help="Quick 5m / 30d / default tickers run")
    parser.add_argument("--universe", default="core", choices=["core", "extended"], help="Ticker universe to test when --ticker is not supplied")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    if args.show:
        show()
        return

    if args.fast:
        args.interval = "5m"
        args.period = "30d"

    tickers = args.ticker or (EXTENDED_TICKERS if args.universe == "extended" else CORE_TICKERS)
    print("\nKTrade Intraday VectorBT")
    print(f"Tickers: {len(tickers)} | interval={args.interval} | period={args.period}")
    print(f"Thresholds: Sharpe>={MIN_SHARPE} WinRate>={MIN_WIN_RATE}% MaxDD<={MAX_DRAWDOWN}% Trades>={MIN_TRADES}\n")

    results = {}
    for ticker in tickers:
        try:
            df = fetch(ticker, args.interval, args.period)
            results[ticker] = process_ticker(ticker, df, args.interval)
            log.info("%s approved=%s", ticker, results[ticker].get("approved_count", 0))
        except Exception as exc:
            log.exception("%s failed", ticker)
            results[ticker] = {"ticker": ticker, "error": str(exc), "strategies": {}}

    approved_store = save_approved(results, args.interval, args.period)
    output = {
        "version": "intraday-v1",
        "run_time": datetime.now().isoformat(),
        "interval": args.interval,
        "period": args.period,
        "thresholds": {
            "min_sharpe": MIN_SHARPE,
            "min_win_rate": MIN_WIN_RATE,
            "max_drawdown": MAX_DRAWDOWN,
            "min_trades": MIN_TRADES,
            "min_profit_factor": MIN_PROFIT_FACTOR,
        },
        "tickers_tested": tickers,
        "results": results,
        "summary": {
            "total_tickers": len(results),
            "total_approved": sum(r.get("approved_count", 0) for r in results.values()),
            "approved_params": str(PARAMS_FILE),
            "approved_tickers": sorted(approved_store.keys()),
        },
    }
    RESULTS_FILE.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    print("\nIntraday VectorBT complete")
    print(f"Approved strategies: {output['summary']['total_approved']}")
    print(f"Report: {RESULTS_FILE}")
    print(f"Params: {PARAMS_FILE}")


if __name__ == "__main__":
    main()




