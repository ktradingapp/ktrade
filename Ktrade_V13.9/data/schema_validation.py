"""
schema_validation.py - KTrade v10.7
===================================
Strict OHLCV frame normalization (#5) and incomplete-last-bar dropping (#8).

Every market frame is normalized before scoring/backtest/risk so that uppercase
columns, string numbers, NaNs, inf, duplicate/out-of-order timestamps, and
impossible OHLC rows cannot silently mis-score the agent.
"""
from __future__ import annotations
from typing import Literal, Optional
import pandas as pd
import numpy as np

try:
    from pydantic import BaseModel, Field
    _HAVE_PYDANTIC = True
except Exception:  # graceful degrade if pydantic missing
    _HAVE_PYDANTIC = False

REQUIRED_OHLCV = ["open", "high", "low", "close", "volume"]
_VALID_INTERVALS = {"1m", "5m", "15m", "1h", "1d", "1w"}

if _HAVE_PYDANTIC:
    class MarketFrameMeta(BaseModel):
        ticker: str = Field(..., min_length=1, max_length=16)
        interval: Literal["1m", "5m", "15m", "1h", "1d", "1w"] = "1d"
        min_rows: int = Field(default=50, ge=2)


def _validate_meta(ticker: str, interval: str, min_rows: int):
    if _HAVE_PYDANTIC:
        m = MarketFrameMeta(ticker=ticker, interval=interval, min_rows=min_rows)
        return m.ticker, m.interval, m.min_rows
    # fallback validation
    if not ticker or len(ticker) > 16:
        raise ValueError(f"invalid ticker: {ticker!r}")
    if interval not in _VALID_INTERVALS:
        interval = "1d"
    return ticker, interval, max(2, int(min_rows))


def normalize_ohlcv_frame(ticker: str, df: pd.DataFrame,
                          interval: str = "1d", min_rows: int = 50) -> pd.DataFrame:
    """Return a clean OHLCV frame or raise ValueError with a precise reason."""
    ticker, interval, min_rows = _validate_meta(ticker, interval, min_rows)

    if df is None or not isinstance(df, pd.DataFrame):
        raise ValueError(f"{ticker}: expected DataFrame, got {type(df).__name__}")

    frame = df.copy()
    frame.columns = [str(c).strip().lower() for c in frame.columns]

    missing = [c for c in REQUIRED_OHLCV if c not in frame.columns]
    if missing:
        raise ValueError(f"{ticker}: missing OHLCV columns: {missing}")

    frame = frame[REQUIRED_OHLCV].copy()
    for col in REQUIRED_OHLCV:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=REQUIRED_OHLCV)
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]

    # Reject impossible OHLC rows (non-positive prices, high<max(o,c,l), low>min(o,c,h))
    bad = (
        (frame["open"] <= 0) | (frame["high"] <= 0) |
        (frame["low"] <= 0) | (frame["close"] <= 0) |
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1)) |
        (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    )
    if bad.any():
        frame = frame.loc[~bad]

    frame["volume"] = frame["volume"].clip(lower=0)

    if len(frame) < min_rows:
        raise ValueError(f"{ticker}: insufficient clean bars: {len(frame)} < {min_rows}")
    return frame


_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 390, "1w": 1950}


def drop_unclosed_last_bar(df: pd.DataFrame, interval: str, now=None) -> pd.DataFrame:
    """Drop the latest bar if it is still forming (#8). Prevents unstable signals
    computed from an unfinished candle whose OHLCV can still change."""
    if df is None or len(df) < 2:
        return df
    try:
        now = pd.Timestamp.utcnow() if now is None else pd.Timestamp(now)
        idx = pd.to_datetime(df.index)
        last_ts = idx[-1]
        # make both tz-naive for subtraction safety
        if getattr(last_ts, "tzinfo", None) is not None:
            last_ts = last_ts.tz_convert("UTC").tz_localize(None)
        if getattr(now, "tzinfo", None) is not None:
            now = now.tz_convert("UTC").tz_localize(None)
        span = _INTERVAL_MINUTES.get(str(interval).lower(), 390)
        age_min = (now - last_ts).total_seconds() / 60.0
        if age_min < span:
            return df.iloc[:-1]
    except Exception:
        return df
    return df
