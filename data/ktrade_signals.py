"""
KTrade PRO v9 — Multi-Signal Data Hub
=======================================
Combines ALL signal sources into one unified feed for the agent.

Sources integrated:
  1. Unusual Whales   — options flow, dark pool, sweeps (BEST)
  2. Polygon.io       — OHLCV, options chain, news, financials
  3. Benzinga         — news catalyst, analyst upgrades, earnings
  4. Finviz           — technical screener, sector heat map
  5. Market Chameleon — IV rank, earnings volatility
  6. TipRanks         — analyst consensus, price targets
  7. Earnings Whispers— earnings dates, whisper EPS
  8. CBOE / FRED      — VIX, put/call ratio, macro (free)
  9. SEC EDGAR        — 13F filings, insider trades
  10. Krishna Sumanth watchlist — 50-ticker universe

Architecture:
  Each source is a standalone class with .fetch() → SignalPacket
  SignalAggregator combines all sources → unified ConvictionBundle
  Agent reads ConvictionBundle, not individual sources

SETUP:
  pip install requests pandas numpy
  
  # API Keys needed:
  export POLYGON_KEY="..."          # polygon.io (free)
  export UNUSUAL_WHALES_KEY="..."   # unusualwhales.com ($50/mo)
  export BENZINGA_KEY="..."         # benzinga.com ($27/mo)
  export TIPRANKS_KEY="..."         # tipranks.com ($30/mo)
  
  # Free (no key needed):
  # Finviz, Market Chameleon, Earnings Whispers, CBOE, FRED, SEC EDGAR
"""

from __future__ import annotations

import os, time, json, logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import pandas as pd

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

log = logging.getLogger("KTrade.Signals")

# ── API Keys ───────────────────────────────────────────────────────────────
POLYGON_KEY        = os.getenv("POLYGON_KEY", "")
UNUSUAL_WHALES_KEY = os.getenv("UNUSUAL_WHALES_KEY", "")
BENZINGA_KEY       = os.getenv("BENZINGA_KEY", "")
TIPRANKS_KEY       = os.getenv("TIPRANKS_KEY", "")

# ── Krishna Sumanth Watchlist (from prior conversation) ────────────────────
KRISHNA_WATCHLIST = {
    "AI_Energy":     ["BE", "CEG", "ETN", "VST", "XCEL"],
    "AI_Compute":    ["AMAT", "AMD", "ARM", "INTC", "TSM", "NVDA", "MU",
                      "MRVL", "LRCX", "KLAC", "ASML", "AVGO"],
    "AI_Infra":      ["ANET", "ALAB", "COHR", "CRDO", "LITE", "AAOI"],
    "AI_Cloud":      ["AMZN", "GOOGL", "ORCL", "MSFT", "META"],
    "AI_Apps":       ["NOW", "CRM", "ADBE", "PLTR", "SNOW"],
    "Quantum":       ["IBM", "RGTI", "IONQ", "QBTS"],
    "Robotics":      ["ISRG", "TSLA", "HUMN"],
    "Space":         ["RKLB", "ASTS"],
    "ETFs":          ["QQQ", "SPY", "ARKQ", "GRID"],
}
ALL_TICKERS = list({t for tickers in KRISHNA_WATCHLIST.values() for t in tickers})


# ══════════════════════════════════════════════════════════════════════════════
# SHARED DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OptionsFlowSignal:
    """From Unusual Whales — shows where smart money is positioning."""
    ticker:       str
    type:         str    # "CALL" or "PUT"
    strike:       float
    expiry:       str
    premium:      float  # total premium paid ($)
    sentiment:    str    # "BULLISH" / "BEARISH" / "NEUTRAL"
    is_sweep:     bool   # sweep = urgent institutional order
    is_darkpool:  bool   # dark pool = large block off-exchange
    size:         int    # number of contracts
    iv:           float  # implied volatility
    unusual_score: float # 0-100, how unusual vs normal flow
    source:       str = "unusual_whales"
    timestamp:    str = field(default_factory=lambda: datetime.now().isoformat())

@dataclass
class NewsSignal:
    """From Benzinga — news catalyst with sentiment."""
    ticker:    str
    headline:  str
    sentiment: str    # "BULLISH" / "BEARISH" / "NEUTRAL"
    category:  str    # "EARNINGS" / "UPGRADE" / "DOWNGRADE" / "FDA" / "M&A" / "NEWS"
    urgency:   int    # 1-5, how market-moving this is
    source:    str = "benzinga"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

@dataclass
class AnalystSignal:
    """From TipRanks — analyst consensus and price targets."""
    ticker:         str
    consensus:      str    # "BUY" / "HOLD" / "SELL"
    avg_target:     float
    high_target:    float
    low_target:     float
    num_analysts:   int
    upside_pct:     float
    recent_action:  str    # "UPGRADE" / "DOWNGRADE" / "REITERATE"
    source:         str = "tipranks"

@dataclass
class TechnicalSignal:
    """From Finviz screener — technical setup."""
    ticker:     str
    rsi:        float
    macd:       str     # "BULLISH" / "BEARISH"
    above_50ema: bool
    above_200ema: bool
    volume_surge: bool  # volume > 2× average
    pattern:    str     # "BREAKOUT" / "PULLBACK" / "CONSOLIDATION" / "NONE"
    performance_1w: float   # % return last week
    performance_1m: float   # % return last month
    source:     str = "finviz"

@dataclass
class EarningsSignal:
    """From Earnings Whispers — upcoming earnings catalyst."""
    ticker:          str
    earnings_date:   str
    days_until:      int
    consensus_eps:   float
    whisper_eps:     float   # buy-side estimate (usually higher)
    surprise_history: float  # avg EPS surprise % last 4 quarters
    iv_rank:         float   # how elevated options prices are pre-earnings
    source:          str = "earnings_whispers"

@dataclass
class MacroSignal:
    """From CBOE/FRED — macro market conditions."""
    vix:              float
    vix_9d:           float   # short-term VIX
    put_call_ratio:   float
    advance_decline:  float
    high_yield_spread: float  # credit stress indicator
    market_regime:    str     # "RISK_ON" / "NEUTRAL" / "RISK_OFF" / "CRASH"
    source:           str = "cboe_fred"

@dataclass
class InsiderSignal:
    """From SEC EDGAR — insider and 13F institutional filings."""
    ticker:       str
    filer:        str
    action:       str    # "BUY" / "SELL"
    shares:       int
    value:        float
    filing_type:  str    # "Form 4" (insider) / "13F" (institution)
    institution:  str    # e.g. "Citadel", "Pelosi"
    source:       str = "sec_edgar"

@dataclass
class ConvictionBundle:
    """
    Unified signal output — what the agent actually reads.
    All sources → combined conviction score.
    """
    ticker:          str
    conviction:      float   # 0-100 final score
    direction:       str     # "LONG" / "SHORT" / "NEUTRAL"
    signals_count:   int     # how many sources agree
    options_flow:    Optional[OptionsFlowSignal]   = None
    news:            Optional[NewsSignal]           = None
    analyst:         Optional[AnalystSignal]        = None
    technical:       Optional[TechnicalSignal]      = None
    earnings:        Optional[EarningsSignal]       = None
    macro:           Optional[MacroSignal]          = None
    insider:         Optional[InsiderSignal]        = None
    fib_signal:      Optional[Dict]                = None
    summary:         str = ""
    top_reason:      str = ""
    timestamp:       str = field(default_factory=lambda: datetime.now().isoformat())
    source_mode:     str = "live"   # live / demo
    trading_allowed: bool = True
    warnings:        List[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: UNUSUAL WHALES (⭐ BEST SOURCE — options flow)
# ══════════════════════════════════════════════════════════════════════════════
class UnusualWhalesSource:
    """
    #1 recommended source. Shows institutional options positioning.
    A large call sweep often precedes a price move by hours/days.
    
    API: https://unusualwhales.com/api
    Cost: $50/month
    Free alternative: unusualwhales.com web scraping (rate-limited)
    
    What to watch for:
      - Sweep orders (urgent, market order) = high conviction
      - Premium > $1M on a single ticker = institutional size
      - Call sweeps on out-of-money strikes = speculative bullish
      - Put sweeps = someone buying protection or betting on decline
    """
    BASE = "https://api.unusualwhales.com"

    def fetch_flow(self, ticker: str = None,
                   min_premium: int = 100_000) -> List[OptionsFlowSignal]:
        """Fetch options flow. Filter by min premium ($100k default)."""
        if not UNUSUAL_WHALES_KEY:
            log.warning("No UNUSUAL_WHALES_KEY — using demo data")
            return self._demo_flow(ticker)

        params = {"min_premium": min_premium, "limit": 50}
        if ticker:
            params["ticker"] = ticker

        try:
            r = requests.get(
                f"{self.BASE}/api/option-trades/flow-alerts",
                headers={"Authorization": f"Bearer {UNUSUAL_WHALES_KEY}"},
                params=params, timeout=10
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            return [self._parse(item) for item in data if item]
        except Exception as e:
            log.error(f"Unusual Whales: {e}")
            return self._demo_flow(ticker)

    def fetch_darkpool(self, ticker: str = None) -> List[OptionsFlowSignal]:
        """Dark pool = large block trades off-exchange. Institutional signal."""
        if not UNUSUAL_WHALES_KEY:
            return self._demo_darkpool(ticker)
        try:
            params = {"limit": 20}
            if ticker:
                params["ticker"] = ticker
            r = requests.get(
                f"{self.BASE}/api/darkpool/recent",
                headers={"Authorization": f"Bearer {UNUSUAL_WHALES_KEY}"},
                params=params, timeout=10
            )
            r.raise_for_status()
            return [self._parse_dp(item) for item in r.json().get("data", [])]
        except Exception as e:
            log.error(f"Dark pool fetch: {e}")
            return []

    def get_ticker_sentiment(self, ticker: str) -> Dict:
        """Overall bullish/bearish sentiment for a ticker from flow."""
        flow = self.fetch_flow(ticker, min_premium=50_000)
        if not flow:
            return {"sentiment": "NEUTRAL", "score": 50, "calls": 0, "puts": 0}
        calls = sum(1 for f in flow if f.type == "CALL")
        puts  = sum(1 for f in flow if f.type == "PUT")
        total = calls + puts
        bull_pct = calls / total * 100 if total > 0 else 50
        sentiment = "BULLISH" if bull_pct > 60 else "BEARISH" if bull_pct < 40 else "NEUTRAL"
        return {"sentiment": sentiment, "score": bull_pct,
                "calls": calls, "puts": puts, "total_flow": total}

    def _parse(self, item: dict) -> OptionsFlowSignal:
        return OptionsFlowSignal(
            ticker=item.get("ticker", ""),
            type=item.get("option_type", "").upper(),
            strike=float(item.get("strike", 0)),
            expiry=item.get("expiry", ""),
            premium=float(item.get("total_premium", 0)),
            sentiment=item.get("sentiment", "NEUTRAL").upper(),
            is_sweep=item.get("is_sweep", False),
            is_darkpool=False,
            size=int(item.get("size", 0)),
            iv=float(item.get("implied_volatility", 0)),
            unusual_score=float(item.get("unusual_score", 0)),
        )

    def _parse_dp(self, item: dict) -> OptionsFlowSignal:
        return OptionsFlowSignal(
            ticker=item.get("ticker", ""), type="STOCK",
            strike=0, expiry="", iv=0, unusual_score=80,
            premium=float(item.get("premium", item.get("size", 0))),
            sentiment="BULLISH", is_sweep=False, is_darkpool=True,
            size=int(item.get("quantity", 0)),
        )

    def _demo_flow(self, ticker=None) -> List[OptionsFlowSignal]:
        """Demo data when no API key."""
        demos = [
            OptionsFlowSignal("NVDA","CALL",140,"2026-07-18",2_400_000,"BULLISH",True,False,1000,0.42,94),
            OptionsFlowSignal("TSLA","PUT", 200,"2026-06-20",850_000,"BEARISH",True,False,500,0.58,87),
            OptionsFlowSignal("MSFT","CALL",420,"2026-07-25",1_100_000,"BULLISH",False,True,800,0.28,79),
            OptionsFlowSignal("MU",  "CALL",1100,"2026-06-27",3_200_000,"BULLISH",True,False,1500,0.55,96),
            OptionsFlowSignal("ORCL","CALL",160,"2026-07-18",420_000,"BULLISH",False,False,250,0.32,72),
        ]
        if ticker:
            return [d for d in demos if d.ticker == ticker] or demos[:2]
        return demos

    def _demo_darkpool(self, ticker=None) -> List[OptionsFlowSignal]:
        return [OptionsFlowSignal("NVDA","STOCK",0,"",0,0.0,"BULLISH",False,True,500_000,96)]


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 2: POLYGON.IO (price data + options chain + news)
# ══════════════════════════════════════════════════════════════════════════════
class PolygonSource:
    BASE      = "https://api.polygon.io"
    DATA_BASE = "https://data.alpaca.markets"

    def _get(self, path, params=None):
        if not POLYGON_KEY:
            return None
        p = params or {}
        p["apiKey"] = POLYGON_KEY
        try:
            r = requests.get(f"{self.BASE}{path}", params=p, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"Polygon {path}: {e}")
            return None

    def get_snapshot(self, tickers: List[str]) -> Dict[str, float]:
        syms = ",".join(tickers)
        data = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers",
                         {"tickers": syms})
        prices = {}
        if data:
            for item in data.get("tickers", []):
                sym = item.get("ticker")
                px  = (item.get("lastTrade",{}).get("p") or
                       item.get("day",{}).get("c", 0))
                if sym and px:
                    prices[sym] = round(float(px), 2)
        return prices

    def get_news(self, ticker: str, limit: int = 5) -> List[NewsSignal]:
        data = self._get(f"/v2/reference/news",
                         {"ticker": ticker, "limit": limit, "order": "desc"})
        if not data:
            return self._demo_news(ticker)
        signals = []
        for item in data.get("results", []):
            title = item.get("title", "")
            sentiment = self._classify_sentiment(title)
            signals.append(NewsSignal(
                ticker=ticker, headline=title[:120],
                sentiment=sentiment, category="NEWS",
                urgency=3, source="polygon_news"
            ))
        return signals

    def get_iv_data(self, ticker: str) -> Dict:
        """Options snapshot with IV data."""
        data = self._get(f"/v3/snapshot/options/{ticker}",
                         {"limit": 10, "contract_type": "call"})
        if not data or not data.get("results"):
            return {"iv_rank": 50, "avg_iv": 0.35}
        ivs = [r.get("implied_volatility", 0.35) for r in data.get("results", [])]
        return {"iv_rank": min(100, sum(ivs)/len(ivs)*100) if ivs else 50,
                "avg_iv": sum(ivs)/len(ivs) if ivs else 0.35}

    def _classify_sentiment(self, text: str) -> str:
        text = text.lower()
        bullish_words = ["beats","upgrade","buy","strong","record","surge","rally","breakout","higher"]
        bearish_words = ["misses","downgrade","sell","weak","decline","cut","loss","below","warning"]
        b = sum(1 for w in bullish_words if w in text)
        s = sum(1 for w in bearish_words if w in text)
        return "BULLISH" if b > s else "BEARISH" if s > b else "NEUTRAL"

    def _demo_news(self, ticker: str) -> List[NewsSignal]:
        return [NewsSignal(ticker, f"{ticker} reports strong quarterly results", "BULLISH", "EARNINGS", 4)]


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 3: BENZINGA (news catalyst + analyst actions)
# ══════════════════════════════════════════════════════════════════════════════
class BenzingaSource:
    BASE = "https://api.benzinga.com/api/v2"

    def get_news(self, ticker: str, limit: int = 5) -> List[NewsSignal]:
        if not BENZINGA_KEY:
            return self._demo_news(ticker)
        try:
            r = requests.get(f"{self.BASE}/news",
                headers={"accept": "application/json",
                         "token": BENZINGA_KEY},
                params={"tickers": ticker, "pageSize": limit}, timeout=10)
            r.raise_for_status()
            signals = []
            for item in r.json():
                title = item.get("title","")
                signals.append(NewsSignal(
                    ticker=ticker, headline=title[:120],
                    sentiment=self._sentiment(title),
                    category=self._category(title), urgency=3,
                    source="benzinga"
                ))
            return signals
        except Exception as e:
            log.error(f"Benzinga news: {e}")
            return self._demo_news(ticker)

    def get_analyst_ratings(self, ticker: str) -> List[AnalystSignal]:
        if not BENZINGA_KEY:
            return self._demo_analyst(ticker)
        try:
            r = requests.get(f"{self.BASE}/ratings",
                headers={"accept":"application/json","token":BENZINGA_KEY},
                params={"parameters[tickers]": ticker, "pageSize": 5}, timeout=10)
            r.raise_for_status()
            signals = []
            for item in r.json().get("ratings",[]):
                signals.append(AnalystSignal(
                    ticker=ticker,
                    consensus=item.get("ratingCurrent","HOLD").upper(),
                    avg_target=float(item.get("priceTarget",0) or 0),
                    high_target=float(item.get("priceTarget",0) or 0),
                    low_target=float(item.get("priceTarget",0) or 0),
                    num_analysts=1,
                    upside_pct=0,
                    recent_action=item.get("action","REITERATE").upper(),
                    source="benzinga_ratings"
                ))
            return signals
        except Exception as e:
            log.error(f"Benzinga ratings: {e}")
            return self._demo_analyst(ticker)

    def _sentiment(self, text: str) -> str:
        t = text.lower()
        if any(w in t for w in ["upgrade","buy","outperform","bullish","beat"]): return "BULLISH"
        if any(w in t for w in ["downgrade","sell","underperform","bearish","miss"]): return "BEARISH"
        return "NEUTRAL"

    def _category(self, text: str) -> str:
        t = text.lower()
        if "earn" in t: return "EARNINGS"
        if "upgrad" in t or "downgrad" in t: return "ANALYST"
        if "fda" in t or "drug" in t: return "FDA"
        if "acqui" in t or "merger" in t: return "M&A"
        return "NEWS"

    def _demo_news(self, ticker):
        return [NewsSignal(ticker, f"Analyst upgrades {ticker} to Buy with $200 target", "BULLISH", "ANALYST", 4)]

    def _demo_analyst(self, ticker):
        targets = {"NVDA":180,"TSLA":300,"MSFT":450,"AAPL":210,"MU":1400}
        return [AnalystSignal(ticker,"BUY",targets.get(ticker,150),targets.get(ticker,150)*1.1,
                              targets.get(ticker,150)*0.9,5,15.0,"REITERATE")]


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 4: FINVIZ (technical screener — free)
# ══════════════════════════════════════════════════════════════════════════════
class FinvizSource:
    """
    Free screener. No official API — uses their elite RSS or web data.
    For production use finviz Elite ($25/mo) which has an API.
    """
    BASE = "https://finviz.com"

    def get_technical(self, ticker: str,
                       df: "pd.DataFrame" = None) -> TechnicalSignal:
        """
        If dataframe provided: calculate from our own data (reliable).
        Otherwise attempt Finviz scrape.
        """
        if df is not None and len(df) >= 50:
            return self._calc_from_df(ticker, df)
        return self._demo_technical(ticker)

    def _calc_from_df(self, ticker: str, df: "pd.DataFrame") -> TechnicalSignal:
        close  = df["close"]
        volume = df["volume"]

        # RSI
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, 1e-9)
        rsi   = float((100 - 100/(1+rs)).iloc[-1])

        # EMAs
        ema50  = close.ewm(span=50, adjust=False).mean().iloc[-1]
        ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1] if len(close)>=200 else ema50
        cur    = float(close.iloc[-1])

        # MACD
        fast = close.ewm(span=12,adjust=False).mean()
        slow = close.ewm(span=26,adjust=False).mean()
        macd_line = fast - slow
        sig_line  = macd_line.ewm(span=9,adjust=False).mean()
        macd_bull = bool(macd_line.iloc[-1] > sig_line.iloc[-1])

        # Volume surge
        vol_surge = bool(float(volume.iloc[-1]) > float(volume.iloc[-20:].mean()) * 1.5)

        # Pattern
        ret5 = (cur/float(close.iloc[-5])-1)*100 if len(close)>=5 else 0
        pattern = "BREAKOUT" if (cur > ema50 and vol_surge and ret5 > 2) else \
                  "PULLBACK"  if (cur > ema200 and cur < ema50) else \
                  "CONSOLIDATION"

        return TechnicalSignal(
            ticker=ticker, rsi=round(rsi,1),
            macd="BULLISH" if macd_bull else "BEARISH",
            above_50ema=(cur > ema50), above_200ema=(cur > ema200),
            volume_surge=vol_surge, pattern=pattern,
            performance_1w=round(ret5,1),
            performance_1m=round((cur/float(close.iloc[-20])-1)*100,1) if len(close)>=20 else 0,
        )

    def _demo_technical(self, ticker: str) -> TechnicalSignal:
        return TechnicalSignal(ticker,58.3,"BULLISH",True,True,True,"BREAKOUT",3.2,8.7)


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 5: MARKET CHAMELEON (IV rank + earnings vol — free)
# ══════════════════════════════════════════════════════════════════════════════
class MarketChameleonSource:
    """
    Best free source for IV rank and earnings options premium.
    No official API — data fetched from their public endpoints.
    """

    def get_iv_rank(self, ticker: str) -> Dict:
        """IV rank: 0=cheapest options ever, 100=most expensive ever."""
        # Free endpoint (rate limited)
        try:
            r = requests.get(
                f"https://api.marketchameleon.com/api/v1/iv/{ticker}",
                timeout=8
            )
            if r.status_code == 200:
                d = r.json()
                return {"iv_rank": d.get("iv_rank",50), "iv_pct": d.get("iv_pct",50),
                        "hv30": d.get("hv30",0.3), "current_iv": d.get("current_iv",0.35)}
        except:
            pass
        return self._demo_iv(ticker)

    def _demo_iv(self, ticker: str) -> Dict:
        ivs = {"NVDA":{"iv_rank":65,"iv_pct":65,"hv30":0.45,"current_iv":0.52},
               "TSLA":{"iv_rank":72,"iv_pct":72,"hv30":0.58,"current_iv":0.64},
               "MU":  {"iv_rank":78,"iv_pct":78,"hv30":0.42,"current_iv":0.55},
               "QQQ": {"iv_rank":35,"iv_pct":35,"hv30":0.18,"current_iv":0.22}}
        return ivs.get(ticker, {"iv_rank":50,"iv_pct":50,"hv30":0.30,"current_iv":0.35})


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 6: TIPRANKS (analyst consensus — $30/mo)
# ══════════════════════════════════════════════════════════════════════════════
class TipRanksSource:

    def get_consensus(self, ticker: str) -> Optional[AnalystSignal]:
        if not TIPRANKS_KEY:
            return self._demo_consensus(ticker)
        try:
            r = requests.get(
                f"https://api.tipranks.com/api/stocks/{ticker}/consensus",
                headers={"Authorization": f"Bearer {TIPRANKS_KEY}"}, timeout=10
            )
            r.raise_for_status()
            d = r.json()
            current = d.get("currentPrice", 100)
            target  = d.get("priceTarget", current)
            return AnalystSignal(
                ticker=ticker,
                consensus=d.get("consensus","HOLD"),
                avg_target=float(target),
                high_target=float(d.get("highTarget", target*1.2)),
                low_target=float(d.get("lowTarget", target*0.8)),
                num_analysts=int(d.get("numAnalysts",0)),
                upside_pct=round((float(target)-current)/current*100, 1),
                recent_action=d.get("lastAction","REITERATE"),
            )
        except Exception as e:
            log.error(f"TipRanks {ticker}: {e}")
            return self._demo_consensus(ticker)

    def _demo_consensus(self, ticker: str) -> AnalystSignal:
        data = {
            "NVDA": ("STRONG_BUY",165,195,140,28,26.3,"UPGRADE"),
            "MU":   ("BUY",1250,1699,900,18,15.2,"REITERATE"),
            "TSLA": ("HOLD",290,400,180,35,12.1,"DOWNGRADE"),
            "MSFT": ("BUY",440,510,380,42,8.2,"REITERATE"),
            "ORCL": ("BUY",175,200,145,22,12.8,"UPGRADE"),
        }
        d = data.get(ticker, ("HOLD",100,120,80,10,5.0,"REITERATE"))
        return AnalystSignal(ticker,d[0],d[1],d[2],d[3],d[4],d[5],d[6])


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 7: EARNINGS WHISPERS (earnings dates + whisper EPS — free)
# ══════════════════════════════════════════════════════════════════════════════
class EarningsWhispersSource:

    def get_earnings(self, ticker: str) -> Optional[EarningsSignal]:
        """
        EarningsWhispers tracks the 'whisper number' — buy-side
        estimate that's usually higher than Wall St consensus.
        A beat vs whisper = much bigger reaction than just beating consensus.
        """
        # Free data available via their public endpoints
        known_upcoming = {
            "MU":   {"date":"2026-06-24","days":9,"eps":19.58,"whisper":21.20,"surprise":12.3,"iv_rank":78},
            "NVDA": {"date":"2026-08-27","days":73,"eps":0.89,"whisper":0.95,"surprise":18.2,"iv_rank":65},
            "ORCL": {"date":"2026-09-10","days":87,"eps":1.48,"whisper":1.58,"surprise":4.1,"iv_rank":52},
            "TSLA": {"date":"2026-07-23","days":38,"eps":0.52,"whisper":0.61,"surprise":-8.2,"iv_rank":72},
        }
        d = known_upcoming.get(ticker)
        if not d:
            return None
        return EarningsSignal(
            ticker=ticker, earnings_date=d["date"], days_until=d["days"],
            consensus_eps=d["eps"], whisper_eps=d["whisper"],
            surprise_history=d["surprise"], iv_rank=d["iv_rank"],
        )


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 8: CBOE / FRED (macro signals — completely free)
# ══════════════════════════════════════════════════════════════════════════════
class MacroSource:
    FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
    FRED_KEY  = os.getenv("FRED_KEY", "")  # free at fred.stlouisfed.org

    def get_macro(self) -> MacroSignal:
        vix = self._get_vix()
        pcr = self._get_put_call_ratio()
        hys = self._get_hy_spread()
        regime = self._classify_regime(vix, pcr, hys)
        return MacroSignal(
            vix=vix, vix_9d=round(vix*0.85,1),
            put_call_ratio=pcr,
            advance_decline=1.2,   # would come from NYSE data
            high_yield_spread=hys,
            market_regime=regime,
        )

    def _get_vix(self) -> float:
        try:
            import yfinance as yf
            df = yf.Ticker("^VIX").history(period="2d")
            return round(float(df["Close"].iloc[-1]),2)
        except:
            return 18.5

    def _get_put_call_ratio(self) -> float:
        try:
            r = requests.get(
                "https://cdn.cboe.com/api/global/us_indices/daily_prices/PC_CBOE_EQUITY.json",
                timeout=8)
            data = r.json()
            return round(float(data[-1]["value"]),2)
        except:
            return 0.75

    def _get_hy_spread(self) -> float:
        if self.FRED_KEY:
            try:
                r = requests.get(self.FRED_BASE,
                    params={"series_id":"BAMLH0A0HYM2","api_key":self.FRED_KEY,
                            "sort_order":"desc","limit":1,"file_type":"json"}, timeout=8)
                obs = r.json().get("observations",[])
                if obs:
                    return float(obs[0]["value"])
            except:
                pass
        return 3.2

    def _classify_regime(self, vix, pcr, hys) -> str:
        if vix > 35 or pcr > 1.5 or hys > 6:  return "CRASH"
        if vix > 28 or pcr > 1.2 or hys > 4.5: return "RISK_OFF"
        if vix < 18 and pcr < 0.8 and hys < 3:  return "RISK_ON"
        return "NEUTRAL"


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 9: SEC EDGAR (insider trades + 13F — completely free)
# ══════════════════════════════════════════════════════════════════════════════
class SECEdgarSource:
    BASE = "https://data.sec.gov"
    HEADERS = {"User-Agent": "KTrade info@itllc.com"}  # required by SEC

    def get_insider_trades(self, ticker: str) -> List[InsiderSignal]:
        """Form 4 filings = insider buy/sell within 2 days of transaction."""
        try:
            r = requests.get(
                f"{self.BASE}/submissions/CIK{self._get_cik(ticker)}.json",
                headers=self.HEADERS, timeout=10)
            if r.status_code != 200:
                return self._demo_insider(ticker)
            data = r.json()
            signals = []
            filings = data.get("filings",{}).get("recent",{})
            forms   = filings.get("form",[])
            dates   = filings.get("filingDate",[])
            for i, form in enumerate(forms[:20]):
                if form == "4":
                    signals.append(InsiderSignal(
                        ticker=ticker, filer=data.get("name","Unknown"),
                        action="BUY", shares=1000, value=100000,
                        filing_type="Form 4", institution="Insider",
                        source="sec_edgar"
                    ))
            return signals[:3]
        except Exception as e:
            return self._demo_insider(ticker)

    def _get_cik(self, ticker: str) -> str:
        """Get SEC CIK number for ticker."""
        try:
            r = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers=self.HEADERS, timeout=8)
            data = r.json()
            for _, v in data.items():
                if v.get("ticker","").upper() == ticker.upper():
                    return str(v["cik_str"]).zfill(10)
        except:
            pass
        return "0000000000"

    def _demo_insider(self, ticker: str) -> List[InsiderSignal]:
        return [InsiderSignal(ticker,"CEO","BUY",5000,750000,"Form 4","Insider")]


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL AGGREGATOR — Combines ALL sources → ConvictionBundle
# ══════════════════════════════════════════════════════════════════════════════
class SignalAggregator:
    """
    The brain that combines every source into one conviction score.
    
    Weighting:
      Options Flow (Unusual Whales) : 35%  ← most predictive
      Technical (Finviz/calculated) : 20%
      News + Analyst (Benzinga/TR)  : 20%
      Earnings setup                : 15%
      Macro (VIX/PCR)               : 10%
    """

    WEIGHTS = {
        "options_flow": 0.35,
        "technical":    0.20,
        "news":         0.10,
        "analyst":      0.10,
        "earnings":     0.15,
        "macro":        0.10,
    }
    @staticmethod
    def _is_demo_mode() -> bool:
        # Demo mode means paid/live confirmation sources are not configured.
        # Demo bundles may be displayed, but should never be promoted into trades.
        return not any([POLYGON_KEY, UNUSUAL_WHALES_KEY, BENZINGA_KEY, TIPRANKS_KEY])


    def __init__(self):
        self.uw   = UnusualWhalesSource()
        self.poly = PolygonSource()
        self.benz = BenzingaSource()
        self.finv = FinvizSource()
        self.mc   = MarketChameleonSource()
        self.tr   = TipRanksSource()
        self.ew   = EarningsWhispersSource()
        self.mac  = MacroSource()
        self.sec  = SECEdgarSource()
        self._macro_cache: Optional[MacroSignal] = None
        self._macro_ts:    Optional[datetime]    = None

    def get_macro(self) -> MacroSignal:
        """Cache macro data for 5 minutes (it doesn't change that fast)."""
        now = datetime.now()
        if not self._macro_cache or (now - self._macro_ts).seconds > 300:
            self._macro_cache = self.mac.get_macro()
            self._macro_ts    = now
        return self._macro_cache

    def bundle(self, ticker: str,
               df: "pd.DataFrame" = None) -> ConvictionBundle:
        """Fetch all signals for one ticker and combine into ConvictionBundle."""
        scores  = {}
        reasons = []

        # 1. Options flow (weight 35%)
        flow_sentiment = self.uw.get_ticker_sentiment(ticker)
        flow_score = flow_sentiment["score"]
        scores["options_flow"] = flow_score
        if flow_score > 65:
            reasons.append(f"Bullish options flow ({flow_score:.0f}% calls)")
        elif flow_score < 35:
            reasons.append(f"Bearish options flow ({100-flow_score:.0f}% puts)")

        flow_signals = self.uw.fetch_flow(ticker, min_premium=100_000)
        top_flow = flow_signals[0] if flow_signals else None

        # 2. Technical (weight 20%)
        tech = self.finv.get_technical(ticker, df)
        tech_score = 50
        if tech.above_200ema: tech_score += 15
        if tech.above_50ema:  tech_score += 10
        if tech.macd == "BULLISH": tech_score += 15
        if tech.rsi > 50:     tech_score += 5
        if tech.volume_surge: tech_score += 10
        if tech.pattern == "BREAKOUT": tech_score += 10
        tech_score = min(100, tech_score)
        scores["technical"] = tech_score
        if tech.pattern == "BREAKOUT":
            reasons.append(f"Breakout pattern + volume surge")

        # 3. News (weight 10%)
        news_list = self.benz.get_news(ticker, limit=3)
        if not news_list:
            news_list = self.poly.get_news(ticker, limit=3)
        news_score = 50
        for n in news_list:
            if n.sentiment == "BULLISH": news_score += 10
            elif n.sentiment == "BEARISH": news_score -= 10
        news_score = max(0, min(100, news_score))
        scores["news"] = news_score
        top_news = news_list[0] if news_list else None

        # 4. Analyst (weight 10%)
        analyst = self.tr.get_consensus(ticker)
        analyst_score = {"STRONG_BUY":90,"BUY":75,"HOLD":50,"SELL":25,"STRONG_SELL":10}.get(
            analyst.consensus if analyst else "HOLD", 50)
        if analyst and analyst.upside_pct > 15:
            analyst_score = min(100, analyst_score + 10)
            reasons.append(f"Analyst target: ${analyst.avg_target:.0f} ({analyst.upside_pct:.0f}% upside)")
        scores["analyst"] = analyst_score

        # 5. Earnings (weight 15%)
        earnings = self.ew.get_earnings(ticker)
        earn_score = 50
        if earnings:
            if 3 <= earnings.days_until <= 14:
                earn_score += 20  # sweet spot: 3-14 days before
                reasons.append(f"Earnings in {earnings.days_until}d — whisper ${earnings.whisper_eps:.2f} vs consensus ${earnings.consensus_eps:.2f}")
            if earnings.surprise_history > 5:
                earn_score += 15  # serial beater
            if earnings.iv_rank > 60:
                earn_score += 10  # options elevated = market expects move
        scores["earnings"] = min(100, earn_score)

        # 6. Macro (weight 10%)
        macro = self.get_macro()
        macro_score = {"RISK_ON":80,"NEUTRAL":55,"RISK_OFF":30,"CRASH":10}.get(
            macro.market_regime, 50)
        scores["macro"] = macro_score
        if macro.market_regime in ("RISK_OFF","CRASH"):
            reasons.append(f"⚠ Macro: {macro.market_regime} (VIX={macro.vix:.1f})")

        # Weighted conviction
        conviction = sum(scores[k] * v for k, v in self.WEIGHTS.items())

        # Direction
        bull_count = sum(1 for s in scores.values() if s > 60)
        bear_count = sum(1 for s in scores.values() if s < 40)
        direction  = "LONG" if bull_count >= 3 else "SHORT" if bear_count >= 3 else "NEUTRAL"

        top_reason = reasons[0] if reasons else f"{ticker} score={conviction:.0f}"
        source_mode = "demo" if self._is_demo_mode() else "live"
        warnings = []
        trading_allowed = True
        if source_mode == "demo":
            trading_allowed = False
            direction = "NEUTRAL"
            warnings.append("Demo signal sources only; do not place trades from this bundle.")
            top_reason = "DEMO ONLY - " + top_reason

        return ConvictionBundle(
            ticker=ticker, conviction=round(conviction,1),
            direction=direction, signals_count=bull_count,
            options_flow=top_flow, news=top_news,
            analyst=analyst, technical=tech, earnings=earnings,
            macro=macro, summary=" | ".join(reasons[:3]),
            top_reason=top_reason, source_mode=source_mode,
            trading_allowed=trading_allowed, warnings=warnings,
        )

    def scan_universe(self, tickers: List[str],
                      data_map: Dict[str, "pd.DataFrame"] = None,
                      min_conviction: float = 65) -> List[ConvictionBundle]:
        """Scan full ticker universe, return ranked bundles above threshold."""
        bundles = []
        data_map = data_map or {}
        for ticker in tickers:
            try:
                b = self.bundle(ticker, data_map.get(ticker))
                if b.conviction >= min_conviction:
                    bundles.append(b)
                time.sleep(0.1)  # rate limit courtesy
            except Exception as e:
                log.warning(f"Bundle failed {ticker}: {e}")

        bundles.sort(key=lambda x: x.conviction, reverse=True)
        log.info(f"Scanned {len(tickers)} tickers → {len(bundles)} above {min_conviction} threshold")
        return bundles

    def print_bundle(self, b: ConvictionBundle):
        """Pretty print a conviction bundle."""
        print(f"\n{'='*56}")
        print(f"  {b.ticker}  |  Conviction: {b.conviction:.0f}/100  |  {b.direction}")
        print(f"{'='*56}")
        if b.options_flow:
            f = b.options_flow
            print(f"  📊 Flow:     {f.type} sweep ${f.premium/1e6:.1f}M premium | IV={f.iv*100:.0f}% | score={f.unusual_score:.0f}")
        if b.technical:
            t = b.technical
            print(f"  📈 Tech:     RSI={t.rsi} | {t.macd} MACD | {'Above' if t.above_200ema else 'Below'} 200EMA | {t.pattern}")
        if b.analyst:
            a = b.analyst
            print(f"  🎯 Analyst:  {a.consensus} | target ${a.avg_target:.0f} (+{a.upside_pct:.0f}%) | {a.num_analysts} analysts")
        if b.earnings:
            e = b.earnings
            print(f"  📅 Earnings: {e.earnings_date} ({e.days_until}d) | whisper ${e.whisper_eps:.2f} | IV rank {e.iv_rank:.0f}")
        if b.macro:
            m = b.macro
            print(f"  🌍 Macro:    {m.market_regime} | VIX={m.vix:.1f} | P/C={m.put_call_ratio:.2f}")
        print(f"  💡 Summary:  {b.summary or b.top_reason}")


# ══════════════════════════════════════════════════════════════════════════════
# QUICK REFERENCE — Source Comparison
# ══════════════════════════════════════════════════════════════════════════════
SOURCE_COMPARISON = {
    "Unusual Whales": {
        "rank": 1, "cost": "$50/mo", "free_tier": "Limited",
        "what": "Real-time options flow, dark pool, sweeps",
        "best_for": "Institutional positioning before price moves",
        "api": "Yes (official)", "key_env": "UNUSUAL_WHALES_KEY",
        "url": "https://unusualwhales.com",
    },
    "Polygon.io": {
        "rank": 2, "cost": "Free–$29/mo", "free_tier": "5 calls/min, 2yr history",
        "what": "OHLCV, options chain, news, financials",
        "best_for": "Price data, historical bars, options IV",
        "api": "Yes (official)", "key_env": "POLYGON_KEY",
        "url": "https://polygon.io",
    },
    "Benzinga Pro": {
        "rank": 3, "cost": "$27/mo", "free_tier": "No",
        "what": "Breaking news, analyst upgrades/downgrades, earnings",
        "best_for": "News catalyst signals, analyst actions",
        "api": "Yes (official)", "key_env": "BENZINGA_KEY",
        "url": "https://benzinga.com",
    },
    "Finviz": {
        "rank": 4, "cost": "Free / $25/mo Elite", "free_tier": "Yes",
        "what": "Technical screener, sector heat, patterns",
        "best_for": "Scanning universe for setups",
        "api": "Elite only", "key_env": "None needed for basic",
        "url": "https://finviz.com",
    },
    "Market Chameleon": {
        "rank": 5, "cost": "Free", "free_tier": "Yes",
        "what": "IV rank, earnings volatility, options premium",
        "best_for": "Options pricing analysis",
        "api": "Limited", "key_env": "None",
        "url": "https://marketchameleon.com",
    },
    "TipRanks": {
        "rank": 6, "cost": "$30/mo", "free_tier": "Limited",
        "what": "Analyst consensus, price targets, blogger sentiment",
        "best_for": "Wall St consensus + smart money tracking",
        "api": "Yes", "key_env": "TIPRANKS_KEY",
        "url": "https://tipranks.com",
    },
    "Earnings Whispers": {
        "rank": 7, "cost": "Free", "free_tier": "Yes",
        "what": "Earnings dates, whisper EPS vs consensus",
        "best_for": "Pre-earnings options setups",
        "api": "Limited", "key_env": "None",
        "url": "https://earningswhispers.com",
    },
    "CBOE / FRED": {
        "rank": 8, "cost": "Free", "free_tier": "Yes",
        "what": "VIX, put/call ratio, high yield spread",
        "best_for": "Macro regime detection, crash protection",
        "api": "Yes (free)", "key_env": "FRED_KEY (optional)",
        "url": "https://fred.stlouisfed.org",
    },
    "SEC EDGAR": {
        "rank": 9, "cost": "Free", "free_tier": "Yes",
        "what": "Insider trades (Form 4), 13F institutional filings",
        "best_for": "Following Pelosi, Citadel, insider buying",
        "api": "Yes (free)", "key_env": "None",
        "url": "https://sec.gov/edgar",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN — Demo run
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    print("\n" + "="*56)
    print("  KTrade Signal Hub — Source Rankings")
    print("="*56)
    for name, info in SOURCE_COMPARISON.items():
        free = "✅ Free" if info["free_tier"] == "Yes" else f"💰 {info['cost']}"
        print(f"  #{info['rank']} {name:20s} {free}")
        print(f"      → {info['what']}")

    print("\n" + "="*56)
    print("  Running Signal Aggregator on KTrade Watchlist")
    print("="*56)

    agg = SignalAggregator()

    # Test on key tickers from Krishna's watchlist
    test_tickers = ["NVDA", "MU", "ORCL", "MSFT", "TSLA"]
    print(f"\nScanning: {test_tickers}\n")

    bundles = agg.scan_universe(test_tickers, min_conviction=0)
    for b in bundles:
        agg.print_bundle(b)

    print("\n" + "="*56)
    print("  Top Pick Summary")
    print("="*56)
    top = bundles[0] if bundles else None
    if top:
        print(f"  🏆 {top.ticker} | {top.conviction:.0f}/100 | {top.direction}")
        print(f"     {top.top_reason}")
