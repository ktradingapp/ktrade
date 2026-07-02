"""Read-only KTrade v9 conviction scan using Alpaca IEX daily bars."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent
for key, value in dotenv_values(PROJECT_DIR / ".env", encoding="utf-8-sig").items():
    if value is not None:
        os.environ[key] = value

from agent.ktrade_agent_v9 import ConvictionScorer


TICKERS = ["SPY", "QQQ", "IWM", "GLD", "TLT", "NVDA", "MSFT", "AAPL"]
DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"


def fetch_daily_bars(symbol: str) -> pd.DataFrame:
    key = os.getenv("ALPACA_KEY", "")
    secret = os.getenv("ALPACA_SECRET", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_KEY and ALPACA_SECRET are required.")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=520)
    response = requests.get(
        DATA_URL.format(symbol=symbol),
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        },
        params={
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustment": "all",
            "feed": "iex",
            "limit": 400,
        },
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json().get("bars", [])
    if not rows:
        raise RuntimeError(f"No Alpaca bars returned for {symbol}.")
    frame = pd.DataFrame(rows).rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    frame.index = pd.to_datetime(frame["t"], utc=True)
    return frame[["open", "high", "low", "close", "volume"]].dropna()


def main() -> None:
    scorer = ConvictionScorer()
    results = []
    for symbol in TICKERS:
        try:
            results.append(scorer.score(symbol, fetch_daily_bars(symbol)))
        except Exception as exc:
            print(f"{symbol:5} ERROR  {exc}")
    print("\nKTrade v9 read-only Alpaca IEX scan")
    print("No orders are submitted.\n")
    for item in sorted(results, key=lambda value: value.score, reverse=True):
        action = "BUY" if item.signal == 1 else "WATCH"
        print(
            f"{item.ticker:5} {action:5} conviction={item.score:5.1f} "
            f"price=${item.price:,.2f} strategy={item.strategy}"
        )


if __name__ == "__main__":
    main()
