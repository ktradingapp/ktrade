"""
KTrade PRO v9 â€” Institutional Data Feed
=========================================
Replaces yfinance with Polygon.io (institutional-grade).

Developer feedback fix #1:
  "Relying on scraped Yahoo Finance data is a major point of failure.
   yfinance is prone to rate limits, silent missing data, and structural changes."

Polygon.io advantages:
  - Official API, not scraped   
  - Survivorship-bias-free historical data (includes delisted tickers)
  - Sub-second websocket feed for real-time quotes
  - Options chain data (Greeks, IV, OI)
  - No rate-limit surprises on paid tier

SETUP:
  pip install requests websocket-client pandas numpy
  export POLYGON_KEY="your_polygon_api_key"
  Free tier: 5 API calls/min, 2yr history
  Starter ($29/mo): unlimited calls, 10yr history, real-time
  
GET KEY: https://polygon.io (free tier is enough to start)
"""

import os, time, logging, json, threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import pandas as pd
import numpy as np

try:
    from dotenv import dotenv_values

    project_dir = Path(__file__).resolve().parent.parent
    for env_key, env_value in dotenv_values(
        project_dir / ".env", encoding="utf-8-sig"
    ).items():
        if env_value is not None:
            os.environ.setdefault(env_key, str(env_value).strip())
except ImportError:
    pass

try:
    import requests
except ImportError:
    raise ImportError("pip install requests")

log = logging.getLogger("KTrade.Data")

DATA_PROVIDER = os.environ.get("KTRADE_DATA_PROVIDER", "auto").strip().lower()
POLYGON_KEY  = os.environ.get("POLYGON_KEY", "") or os.environ.get("POLYGON_API_KEY", "")
POLYGON_BASE = "https://api.polygon.io"
ALPACA_KEY   = os.environ.get("ALPACA_KEY", "") or os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET= os.environ.get("ALPACA_SECRET", "") or os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_DATA  = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
FINNHUB_KEY  = os.environ.get("FINNHUB_API_KEY", "") or os.environ.get("FINNHUB_KEY", "")
FINNHUB_BASE = "https://finnhub.io/api/v1"

# â”€â”€ Fallback: yfinance (with warnings) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    import yfinance as yf
    YFINANCE_OK = True
except ImportError:
    YFINANCE_OK = False


# â”€â”€ Polygon REST helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _polygon_get(path: str, params: dict = None) -> Optional[dict]:
    if not POLYGON_KEY:
        return None
    p = params or {}
    p["apiKey"] = POLYGON_KEY
    try:
        r = requests.get(f"{POLYGON_BASE}{path}", params=p, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "request failed")
        log.error(f"Polygon GET {path}: HTTP {status}")
        return None




def _alpaca_get(path: str, params: dict = None) -> Optional[dict]:
    if not ALPACA_KEY or not ALPACA_SECRET:
        return None
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    try:
        r = requests.get(f"{ALPACA_DATA}{path}", params=params or {}, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "request failed")
        log.error(f"Alpaca GET {path}: HTTP {status}")
        return None


def _finnhub_get(path: str, params: dict = None) -> Optional[dict]:
    """Finnhub REST helper for quote, candle, and news fallback data."""
    if not FINNHUB_KEY:
        return None
    p = dict(params or {})
    p["token"] = FINNHUB_KEY
    try:
        r = requests.get(f"{FINNHUB_BASE}{path}", params=p, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "request failed")
        log.error(f"Finnhub GET {path}: HTTP {status}")
        return None

# v10.2: bad-tick / decimal-shift guard on live quotes
try:
    from price_sanity import PRICE_GUARD
except ImportError:
    try:
        from data.price_sanity import PRICE_GUARD
    except ImportError:
        PRICE_GUARD = None


class PolygonDataFeed:
    """
    Primary data feed using Polygon.io.
    Automatically falls back to yfinance if no Polygon key.
    """

    def __init__(self):
        self._cache: Dict[str, pd.DataFrame] = {}
        self._cache_ts: Dict[str, datetime]  = {}
        self.cache_ttl_minutes = 5
        valid_sources = {"polygon", "alpaca", "finnhub", "yfinance"}
        if DATA_PROVIDER in valid_sources:
            self.source = DATA_PROVIDER
        else:
            self.source = (
                "polygon" if POLYGON_KEY else (
                    "alpaca" if ALPACA_KEY and ALPACA_SECRET else (
                        "finnhub" if FINNHUB_KEY else ("yfinance" if YFINANCE_OK else "none")
                    )
                )
            )
        log.info(f"DataFeed source: {self.source.upper()}")
        if not POLYGON_KEY and not FINNHUB_KEY:
            log.warning("No Polygon or Finnhub key found; using lower-quality fallback data if available")

    def get_bars(self, ticker: str, days: int = 252,
                 interval: str = "1d") -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV bars. Interval: '1d', '1h', '5m', '1m'
        Returns DataFrame with columns: open, high, low, close, volume
        """
        cache_key = f"{ticker}_{interval}"
        # Return cache if fresh
        if cache_key in self._cache:
            age = (datetime.now() - self._cache_ts[cache_key]).total_seconds() / 60
            if age < self.cache_ttl_minutes:
                return self._cache[cache_key]

        df = None
        use_auto = DATA_PROVIDER == "auto"
        if self.source == "polygon" or (use_auto and POLYGON_KEY):
            df = self._polygon_bars(ticker, days, interval)
        if df is None and (self.source == "alpaca" or (use_auto and ALPACA_KEY and ALPACA_SECRET)):
            df = self._alpaca_bars(ticker, days, interval)
        if df is None and (self.source == "finnhub" or (use_auto and FINNHUB_KEY)):
            df = self._finnhub_bars(ticker, days, interval)
        if df is None and (self.source == "yfinance" or (use_auto and YFINANCE_OK)):
            df = self._yfinance_bars(ticker, days, interval)

        if df is not None and len(df) > 0:
            self._cache[cache_key]    = df
            self._cache_ts[cache_key] = datetime.now()
        return df

    def _polygon_bars(self, ticker: str, days: int, interval: str) -> Optional[pd.DataFrame]:
        # Map interval to Polygon multiplier/timespan
        span_map = {"1m":"minute","5m":"minute","15m":"minute","1h":"hour","1d":"day","1w":"week"}
        mult_map = {"1m":1,"5m":5,"15m":15,"1h":1,"1d":1,"1w":1}
        timespan = span_map.get(interval, "day")
        mult     = mult_map.get(interval, 1)

        end   = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 50)).strftime("%Y-%m-%d")

        data = _polygon_get(
            f"/v2/aggs/ticker/{ticker}/range/{mult}/{timespan}/{start}/{end}",
            {"adjusted": "true", "sort": "asc", "limit": 50000}
        )
        if not data or data.get("resultsCount", 0) == 0:
            return None

        rows = data.get("results", [])
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
        df = df.set_index("timestamp")
        df = df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume","vw":"vwap"})
        df = df[["open","high","low","close","volume"]].dropna()
        log.debug(f"Polygon: {ticker} {interval} â†’ {len(df)} bars")
        return df


    def _alpaca_bars(self, ticker: str, days: int, interval: str) -> Optional[pd.DataFrame]:
        """Fetch OHLCV bars from Alpaca market data as fallback after Polygon."""
        tf_map = {"1m":"1Min", "5m":"5Min", "15m":"15Min", "1h":"1Hour", "1d":"1Day"}
        timeframe = tf_map.get(interval, "1Day")
        end_dt = datetime.utcnow()
        start_dt = end_dt - timedelta(days=days + 50)
        params = {
            "symbols": ticker,
            "timeframe": timeframe,
            "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "adjustment": "all",
            "limit": 10000,
        }
        feed = os.environ.get("ALPACA_DATA_FEED", "").strip()
        if feed:
            params["feed"] = feed
        data = _alpaca_get("/v2/stocks/bars", params)
        if not data:
            return None
        rows = data.get("bars", {}).get(ticker, [])
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["t"], utc=True)
        df = df.set_index("timestamp")
        df = df.rename(columns={"o":"open", "h":"high", "l":"low", "c":"close", "v":"volume"})
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        log.warning(f"Using Alpaca fallback for {ticker} {interval} -> {len(df)} bars")
        return df

    def _finnhub_bars(self, ticker: str, days: int, interval: str) -> Optional[pd.DataFrame]:
        """Fetch OHLCV bars from Finnhub as an optional fallback/provider."""
        resolution_map = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "1d": "D", "1w": "W"}
        resolution = resolution_map.get(interval, "D")
        end_ts = int(time.time())
        start_ts = int((datetime.now() - timedelta(days=days + 50)).timestamp())
        data = _finnhub_get("/stock/candle", {
            "symbol": ticker,
            "resolution": resolution,
            "from": start_ts,
            "to": end_ts,
        })
        if not data or data.get("s") != "ok":
            return None
        try:
            df = pd.DataFrame({
                "timestamp": pd.to_datetime(data["t"], unit="s", utc=True),
                "open": data["o"],
                "high": data["h"],
                "low": data["l"],
                "close": data["c"],
                "volume": data["v"],
            }).set_index("timestamp")
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            log.warning(f"Using Finnhub fallback for {ticker} {interval} -> {len(df)} bars")
            return df.tail(days + 50)
        except Exception as e:
            log.error(f"Finnhub candle parse failed for {ticker}: {e}")
            return None

    def _yfinance_bars(self, ticker: str, days: int, interval: str) -> Optional[pd.DataFrame]:
        log.warning(f"Using yfinance fallback for {ticker} â€” less reliable")
        yf_interval_map = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h","1d":"1d","1w":"1wk"}
        yf_period_map   = {"1m":"7d","5m":"60d","15m":"60d","1h":"730d","1d":"max","1w":"max"}
        yf_interval = yf_interval_map.get(interval, "1d")
        yf_period   = yf_period_map.get(interval, "2y")
        try:
            tk = yf.Ticker(ticker)
            df = tk.history(period=yf_period, interval=yf_interval, auto_adjust=True)
            df.columns = [c.lower() for c in df.columns]
            df = df[["open","high","low","close","volume"]].dropna()
            return df.tail(days + 50)
        except Exception as e:
            log.error(f"yfinance failed for {ticker}: {e}")
            return None

    def get_snapshot(self, tickers: List[str]) -> Dict[str, float]:
        """Live price snapshot for multiple tickers."""
        prices = {}
        use_auto = DATA_PROVIDER == "auto"
        if self.source == "polygon" or (use_auto and POLYGON_KEY):
            prices.update(self._polygon_snapshot(tickers))
        missing = [ticker for ticker in tickers if ticker not in prices]
        if missing and (self.source == "alpaca" or (use_auto and ALPACA_KEY and ALPACA_SECRET)):
            prices.update(self._alpaca_snapshot(missing))
        missing = [ticker for ticker in tickers if ticker not in prices]
        if missing and (self.source == "finnhub" or (use_auto and FINNHUB_KEY)):
            prices.update(self._finnhub_snapshot(missing))
        missing = [ticker for ticker in tickers if ticker not in prices]
        if missing and (self.source == "yfinance" or (use_auto and YFINANCE_OK)):
            prices.update(self._yfinance_snapshot(missing))
        if PRICE_GUARD is not None:
            prices = PRICE_GUARD.scrub(prices)   # v10.2: drop/flag bad ticks
        return prices



    def get_alpaca_reference_prices(self, tickers: List[str]) -> Dict[str, float]:
        """Fetch broker-side Alpaca stock snapshots only for price validation.
        This intentionally does not fall back to yfinance/Polygon, because it is
        used to detect bad scanner prices before a BUY action is allowed.
        """
        if not ALPACA_KEY or not ALPACA_SECRET:
            return {}
        clean = [t for t in tickers if t and "-" not in t and not str(t).startswith("X:")]
        if not clean:
            return {}
        return self._alpaca_snapshot(clean)

    def _alpaca_snapshot(self, tickers: List[str]) -> Dict[str, float]:
        prices = {}
        data = _alpaca_get("/v2/stocks/snapshots", {"symbols": ",".join(tickers)})
        if not data:
            return prices
        for sym, snap in data.items():
            try:
                px = (snap.get("latestTrade") or {}).get("p") or (snap.get("latestQuote") or {}).get("ap") or (snap.get("dailyBar") or {}).get("c")
                if px:
                    prices[sym] = round(float(px), 2)
            except Exception:
                pass
        return prices

    def _polygon_snapshot(self, tickers: List[str]) -> Dict[str, float]:
        syms = ",".join(tickers)
        data = _polygon_get(f"/v2/snapshot/locale/us/markets/stocks/tickers",
                            {"tickers": syms})
        prices = {}
        if data:
            for item in data.get("tickers", []):
                sym = item.get("ticker")
                px  = item.get("lastTrade", {}).get("p") or \
                      item.get("lastQuote", {}).get("P") or \
                      item.get("day", {}).get("c", 0)
                if sym and px:
                    prices[sym] = round(float(px), 2)

        # Polygon's free plan may deny the bulk snapshot endpoint. Use the
        # permitted daily aggregate endpoint for any missing symbols.
        missing = [ticker for ticker in tickers if ticker not in prices]
        delay = float(os.getenv("POLYGON_SCAN_DELAY_SECONDS", "13"))
        for index, ticker in enumerate(missing):
            bars = self.get_bars(ticker, days=7, interval="1d")
            if bars is not None and len(bars):
                prices[ticker] = round(float(bars["close"].iloc[-1]), 2)
            if index < len(missing) - 1:
                time.sleep(delay)
        return prices

    def _finnhub_snapshot(self, tickers: List[str]) -> Dict[str, float]:
        prices = {}
        for ticker in tickers:
            data = _finnhub_get("/quote", {"symbol": ticker})
            if not data:
                continue
            try:
                px = data.get("c") or data.get("pc")
                if px:
                    prices[ticker] = round(float(px), 2)
            except Exception:
                pass
        return prices

    def _yfinance_snapshot(self, tickers: List[str]) -> Dict[str, float]:
        prices = {}
        for t in tickers:
            try:
                df = yf.Ticker(t).history(period="1d", interval="1m")
                if len(df):
                    prices[t] = round(float(df["Close"].iloc[-1]), 2)
            except:
                pass
        return prices

    def get_options_chain(self, ticker: str, expiry: str = None) -> Optional[pd.DataFrame]:
        """Fetch options chain with Greeks and IV (Polygon only)."""
        if not POLYGON_KEY:
            log.warning("Options chain requires Polygon.io key")
            return None
        params = {"underlying_ticker": ticker, "limit": 250}
        if expiry:
            params["expiration_date"] = expiry
        data = _polygon_get("/v3/reference/options/contracts", params)
        if not data:
            return None
        rows = data.get("results", [])
        return pd.DataFrame(rows) if rows else None

    def get_vix(self) -> float:
        """Fetch current VIX level."""
        if POLYGON_KEY:
            data = _polygon_get("/v2/aggs/ticker/I:VIX/range/1/day/"
                                f"{(datetime.now()-timedelta(days=5)).strftime('%Y-%m-%d')}/"
                                f"{datetime.now().strftime('%Y-%m-%d')}",
                                {"sort":"desc","limit":1})
            if data and data.get("results"):
                return float(data["results"][0]["c"])
        # Fallback
        if YFINANCE_OK:
            try:
                df = yf.Ticker("^VIX").history(period="1d")
                return float(df["Close"].iloc[-1])
            except:
                pass
        return 18.0  # default if all fail

    def get_put_call_ratio(self) -> float:
        """CBOE put/call ratio via Polygon."""
        if not POLYGON_KEY:
            return 0.8  # neutral default
        data = _polygon_get("/v2/aggs/ticker/I:PCRA/range/1/day/"
                            f"{(datetime.now()-timedelta(days=5)).strftime('%Y-%m-%d')}/"
                            f"{datetime.now().strftime('%Y-%m-%d')}",
                            {"sort":"desc","limit":1})
        if data and data.get("results"):
            return float(data["results"][0]["c"])
        return 0.8

    def batch_get(self, tickers: List[str], days: int = 252,
                  interval: str = "1d") -> Dict[str, pd.DataFrame]:
        """Fetch bars for multiple tickers. Respects rate limits."""
        result = {}
        delay  = 0.2 if POLYGON_KEY else 0.5  # Polygon is faster
        for i, ticker in enumerate(tickers):
            df = self.get_bars(ticker, days, interval)
            if df is not None and len(df) >= 20:
                result[ticker] = df
            if i % 10 == 9:
                time.sleep(delay)
        log.info(f"Batch fetch: {len(result)}/{len(tickers)} tickers loaded")
        return result


# â”€â”€ Fibonacci Extension Calculator (from MU screenshot) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class FibonacciExtensionAnalyzer:
    """
    Calculates Fibonacci extension targets post-ATH break.
    Based on the MU analysis screenshot:
      If MU clears $1089.29 (ATH):
        $1322 (1.236) â€” first target
        $1466 (1.382) â€” momentum continuation
        $1582 (1.500) â€” Wolfe zone
        $1699 (1.618â˜…) â€” golden ratio / Daiwa alignment
        $2075 (2.000) â€” full extension / UBS target

    This is now a SIGNAL SOURCE in the agent â€” if price breaks ATH,
    the agent uses these Fibonacci levels as targets for bracket orders.
    """
    # Standard Fibonacci extension levels
    EXTENSION_LEVELS = [1.236, 1.382, 1.500, 1.618, 2.000, 2.618]
    LEVEL_LABELS     = {
        1.236: "First Target",
        1.382: "Momentum Continuation",
        1.500: "Wolfe Zone",
        1.618: "Golden Ratio â˜… (Strongest)",
        2.000: "Full Extension",
        2.618: "Extreme Extension",
    }

    def calculate_extensions(self, swing_low: float, swing_high: float,
                              retracement_low: float = None) -> Dict:
        """
        Calculate extension targets from a swing lowâ†’highâ†’retracement pattern.
        If retracement_low not provided, uses swing_low.

        MU example:
          swing_low  = prior significant low
          swing_high = ATH ($1089.29)
          Extensions calculated above swing_high
        """
        base = retracement_low or swing_low
        move = swing_high - base
        targets = {}
        for level in self.EXTENSION_LEVELS:
            price = swing_high + (move * (level - 1.0))
            targets[level] = {
                "price":   round(price, 2),
                "label":   self.LEVEL_LABELS.get(level, f"{level}x"),
                "pct_from_high": round((price - swing_high) / swing_high * 100, 1)
            }
        return targets

    def get_ath_signal(self, ticker: str, df: pd.DataFrame,
                       ath: float = None) -> Optional[Dict]:
        """
        Returns signal data if price has just broken ATH.
        Adds Fibonacci extension targets as bracket order targets.
        """
        if df is None or len(df) < 50:
            return None

        current_price = df["close"].iloc[-1]
        rolling_ath   = df["high"].rolling(252).max().iloc[-1] if ath is None else ath

        # ATH break condition: today's close > 252-day high
        ath_break = current_price >= rolling_ath * 0.999  # within 0.1% of ATH

        if not ath_break:
            return None

        # Calculate swing for extensions
        swing_low = df["low"].rolling(50).min().iloc[-1]
        extensions = self.calculate_extensions(swing_low, rolling_ath)

        # Primary target = 1.618 (golden ratio) â€” strongest institutional level
        primary_target = extensions[1.618]["price"]
        first_target   = extensions[1.236]["price"]

        return {
            "ticker":         ticker,
            "signal":         "ATH_BREAK",
            "current_price":  round(current_price, 2),
            "ath":            round(rolling_ath, 2),
            "conviction":     88,  # ATH breaks are high-conviction
            "strategy":       "Fibonacci Extension Post-ATH",
            "first_target":   first_target,
            "primary_target": primary_target,
            "extensions":     extensions,
            "note":           f"Price cleared ATH ${rolling_ath:.2f} â€” Fib targets active",
        }

    def mu_example(self) -> Dict:
        """The exact MU analysis from the screenshot."""
        return {
            "ticker": "MU",
            "ath":    1089.29,
            "earnings_date": "Jun 24",
            "eps_consensus": 19.58,
            "rev_consensus": "34.3B",
            "condition": "If MU clears $1089.29",
            "extensions": {
                1.236: {"price": 1322, "label": "First target ~14% above ATH"},
                1.382: {"price": 1466, "label": "Momentum continuation"},
                1.500: {"price": 1582, "label": "Wolfe $1,250 zone"},
                1.618: {"price": 1699, "label": "Daiwa $1,600 aligns exactly â˜…"},
                2.000: {"price": 2075, "label": "UBS $1,625 / full range"},
            },
            "key_binary": "Earnings Jun 24 â€” consensus EPS ~$19.58, Rev ~$34.3B"
        }


# â”€â”€ Persistent State Store (fixes dev feedback #2: in-memory state loss) â”€â”€
class StateStore:
    """
    Simple JSON-based persistence for positions and agent state.
    Fixes: "a simple server crash wipes its immediate working memory"
    
    In production: swap json_path for PostgreSQL/Redis connection.
    """
    def __init__(self, path: str = "ktrade_state.json"):
        self.path = path
        self._state = self._load()
        log.info(f"StateStore loaded from {path}: {len(self._state.get('positions',{}))} positions")

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"positions": {}, "daily_pnl": 0.0,
                    "equity_open": 0.0, "orders": [], "last_saved": ""}

    def save(self):
        self._state["last_saved"] = datetime.now().isoformat()
        try:
            with open(self.path, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
        except Exception as e:
            log.error(f"StateStore save failed: {e}")

    def get(self, key: str, default=None):
        return self._state.get(key, default)

    def set(self, key: str, value):
        self._state[key] = value
        self.save()

    def add_position(self, ticker: str, data: dict):
        positions = self._state.get("positions", {})
        positions[ticker] = {**data, "added_at": datetime.now().isoformat()}
        self._state["positions"] = positions
        self.save()

    def remove_position(self, ticker: str):
        positions = self._state.get("positions", {})
        positions.pop(ticker, None)
        self._state["positions"] = positions
        self.save()

    def get_positions(self) -> dict:
        return self._state.get("positions", {})


# â”€â”€ Main entry for testing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    feed = PolygonDataFeed()
    fib  = FibonacciExtensionAnalyzer()
    store = StateStore()

    print("\n=== MU Fibonacci Extension Targets (from screenshot) ===")
    mu = fib.mu_example()
    print(f"Condition: {mu['condition']}")
    print(f"Key binary: {mu['key_binary']}")
    for level, data in mu["extensions"].items():
        print(f"  {level:.3f} â†’ ${data['price']:,}  ({data['label']})")

    print("\n=== VIX Level ===")
    vix = feed.get_vix()
    print(f"Current VIX: {vix:.1f}")

    print("\n=== Snapshot (SPY, QQQ, MU) ===")
    snaps = feed.get_snapshot(["SPY", "QQQ", "MU"])
    for t, p in snaps.items():
        print(f"  {t}: ${p:.2f}")

    print("\n=== State Store ===")
    print(f"  Positions: {store.get_positions()}")
    print(f"  Last saved: {store.get('last_saved', 'never')}")





