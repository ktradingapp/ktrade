"""
KTrade PRO v9 — Full Universe Scanner
=======================================
Scans ALL stocks across ALL 9 signal sources in parallel.

NO single-ticker limitation — runs on the entire universe simultaneously.

Usage:
    python ktrade_scanner.py                    # full scan, all sources
    python ktrade_scanner.py --top 20           # show top 20 only
    python ktrade_scanner.py --sector AI_Compute # one sector
    python ktrade_scanner.py --ticker MU NVDA   # specific tickers
    python ktrade_scanner.py --min-score 75     # high conviction only
    python ktrade_scanner.py --export           # save results to CSV/JSON
    python ktrade_scanner.py --watch            # live refresh every 5 min

Architecture:
    UniverseScanner
        → ThreadPoolExecutor (parallel per ticker)
            → SignalAggregator.bundle(ticker)  ← all 9 sources per ticker
        → RankedResults (sorted by conviction)
        → Export / Feed to agent

Speed:
    Sequential (old): 200 tickers × 9 sources × 0.5s = ~15 minutes
    Parallel (this):  200 tickers × 9 sources / 20 workers = ~45 seconds
"""

from __future__ import annotations

import os, sys, time, json, csv, logging, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Callable
import threading

try:
    import requests
    import pandas as pd
    import numpy as np
except ImportError as e:
    print(f"pip install requests pandas numpy — missing: {e}")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Import signal sources from ktrade_signals.py ──────────────────────────
try:
    sys.path.insert(0, os.path.dirname(__file__))
    from ktrade_signals import (
        SignalAggregator, ConvictionBundle,
        UnusualWhalesSource, PolygonSource, BenzingaSource,
        FinvizSource, MarketChameleonSource, TipRanksSource,
        EarningsWhispersSource, MacroSource, SECEdgarSource,
        KRISHNA_WATCHLIST, ALL_TICKERS,
        OptionsFlowSignal, NewsSignal, AnalystSignal,
        TechnicalSignal, EarningsSignal, MacroSignal, InsiderSignal,
    )
    SIGNALS_IMPORTED = True
except ImportError as e:
    print(f"⚠ ktrade_signals.py not found ({e}) — running standalone")
    SIGNALS_IMPORTED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ktrade_scanner.log", mode="a"),
    ],
)
log = logging.getLogger("KTrade.Scanner")

# ── API Keys ──────────────────────────────────────────────────────────────
POLYGON_KEY        = os.getenv("POLYGON_KEY", "")
UNUSUAL_WHALES_KEY = os.getenv("UNUSUAL_WHALES_KEY", "")
BENZINGA_KEY       = os.getenv("BENZINGA_KEY", "")
TIPRANKS_KEY       = os.getenv("TIPRANKS_KEY", "")

# ══════════════════════════════════════════════════════════════════════════
# FULL TICKER UNIVERSE — Every stock the agent monitors
# ══════════════════════════════════════════════════════════════════════════
UNIVERSE = {
    # ── Krishna Sumanth Watchlist ─────────────────────────────────────────
    "AI_Energy":     ["BE", "CEG", "ETN", "VST", "XCEL", "NRG", "NEE", "AES"],
    "AI_Compute":    ["NVDA", "AMD", "AMAT", "ARM", "INTC", "TSM", "MU",
                      "MRVL", "LRCX", "KLAC", "ASML", "AVGO", "SMCI", "ON",
                      "MPWR", "TXN", "ADI", "SWKS", "QRVO", "QCOM"],
    "AI_Infra":      ["ANET", "ALAB", "COHR", "CRDO", "LITE", "AAOI",
                      "VIAV", "CIEN", "CSCO", "JNPR", "FFIV", "NET"],
    "AI_Cloud":      ["MSFT", "AMZN", "GOOGL", "META", "ORCL",
                      "SNOW", "MDB", "DDOG", "ESTC", "CFLT"],
    "AI_Apps":       ["NOW", "CRM", "ADBE", "PLTR", "PATH",
                      "AI", "BBAI", "SOUN", "GFAI", "AGEN"],
    "AI_DevTools":   ["GTLB", "HUBS", "TEAM", "ZM", "DOCU", "BOX"],
    "Quantum":       ["IBM", "RGTI", "IONQ", "QBTS", "QUBT", "ARQQ"],
    "Robotics":      ["ISRG", "TSLA", "HUMN", "ABB", "FANUY", "ROK"],
    "Space":         ["RKLB", "ASTS", "SPCE", "LUNR", "RDW", "BWXT"],
    "Semiconductor": ["WOLF", "ONTO", "ACLS", "COHU", "FORM", "ICHR"],
    "Cybersecurity": ["CRWD", "PANW", "ZS", "S", "FTNT", "OKTA", "CYBR"],
    "Fintech":       ["COIN", "HOOD", "SQ", "AFRM", "UPST", "LC"],
    "Biotech":       ["MRNA", "BNTX", "NVAX", "RXRX", "SANA", "BEAM"],
    "EV":            ["RIVN", "LCID", "NIO", "XPEV", "LI", "BLNK"],
    "Large_Cap":     ["AAPL", "NFLX", "INTU", "UBER", "LYFT", "ABNB"],
    "Financials":    ["JPM", "GS", "MS", "BAC", "BX", "KKR", "APO"],
    "ETFs":          ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF",
                      "XLE", "XLV", "XLI", "GLD", "ARKQ", "GRID",
                      "ARKK", "ARKX", "DRAM", "AIPO"],
}

# Flat list of all unique tickers
ALL_UNIVERSE_TICKERS = list(dict.fromkeys(
    t for tickers in UNIVERSE.values() for t in tickers
))

# Sector lookup: ticker → sector
TICKER_TO_SECTOR = {
    t: sector
    for sector, tickers in UNIVERSE.items()
    for t in tickers
}

log.info(f"Universe loaded: {len(ALL_UNIVERSE_TICKERS)} tickers across {len(UNIVERSE)} sectors")


# ══════════════════════════════════════════════════════════════════════════
# RATE LIMITER — per-source throttling to avoid bans
# ══════════════════════════════════════════════════════════════════════════
class RateLimiter:
    """Thread-safe rate limiter per API source."""
    # calls_per_minute per source (conservative)
    LIMITS = {
        "polygon":       200,
        "unusual_whales": 60,
        "benzinga":       30,
        "finviz":         20,
        "market_chameleon": 15,
        "tipranks":       20,
        "earnings_whispers": 10,
        "cboe_fred":      30,
        "sec_edgar":      10,
    }

    def __init__(self):
        self._locks:    Dict[str, threading.Lock]  = {}
        self._counters: Dict[str, List[float]]     = {}
        for src in self.LIMITS:
            self._locks[src]    = threading.Lock()
            self._counters[src] = []

    def wait(self, source: str):
        """Block until we're within rate limit for this source."""
        limit = self.LIMITS.get(source, 30)
        lock  = self._locks.get(source, threading.Lock())
        with lock:
            now   = time.time()
            calls = self._counters.get(source, [])
            # Remove calls older than 60 seconds
            calls = [t for t in calls if now - t < 60]
            if len(calls) >= limit:
                sleep_for = 60 - (now - calls[0]) + 0.1
                log.debug(f"Rate limit {source}: sleeping {sleep_for:.1f}s")
                time.sleep(sleep_for)
                calls = []
            calls.append(time.time())
            self._counters[source] = calls

RATE_LIMITER = RateLimiter()


# ══════════════════════════════════════════════════════════════════════════
# SIGNAL CACHE — avoid re-fetching same data in same scan
# ══════════════════════════════════════════════════════════════════════════
class SignalCache:
    """Thread-safe TTL cache for signal data."""
    def __init__(self, ttl_seconds: int = 300):
        self._data:  Dict[str, Any]      = {}
        self._times: Dict[str, float]    = {}
        self._lock  = threading.Lock()
        self._ttl   = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key in self._data:
                if time.time() - self._times[key] < self._ttl:
                    return self._data[key]
                del self._data[key]
                del self._times[key]
        return None

    def set(self, key: str, value: Any):
        with self._lock:
            self._data[key]  = value
            self._times[key] = time.time()

    def clear(self):
        with self._lock:
            self._data.clear()
            self._times.clear()

CACHE = SignalCache(ttl_seconds=300)


# ══════════════════════════════════════════════════════════════════════════
# PARALLEL BUNDLE FETCHER — all 9 sources per ticker, all tickers at once
# ══════════════════════════════════════════════════════════════════════════
class ParallelBundleFetcher:
    """
    Fetches all 9 signal sources for every ticker in the universe,
    in parallel using ThreadPoolExecutor.
    
    Inner parallelism: per-ticker, each source fetched concurrently
    Outer parallelism: multiple tickers processed at once
    """

    def __init__(self, max_workers: int = 20):
        self.max_workers = max_workers
        # Source instances (shared, thread-safe reads)
        self.uw   = UnusualWhalesSource()   if SIGNALS_IMPORTED else None
        self.poly = PolygonSource()         if SIGNALS_IMPORTED else None
        self.benz = BenzingaSource()        if SIGNALS_IMPORTED else None
        self.finv = FinvizSource()          if SIGNALS_IMPORTED else None
        self.mc   = MarketChameleonSource() if SIGNALS_IMPORTED else None
        self.tr   = TipRanksSource()        if SIGNALS_IMPORTED else None
        self.ew   = EarningsWhispersSource() if SIGNALS_IMPORTED else None
        self.mac  = MacroSource()           if SIGNALS_IMPORTED else None
        self.sec  = SECEdgarSource()        if SIGNALS_IMPORTED else None
        self._macro_signal = None           # shared macro, fetched once
        self._progress_lock = threading.Lock()
        self._completed     = 0

    def _fetch_macro_once(self) -> Optional[Any]:
        """Macro is the same for all tickers — fetch once and share."""
        cached = CACHE.get("macro_signal")
        if cached:
            return cached
        try:
            RATE_LIMITER.wait("cboe_fred")
            macro = self.mac.get_macro()
            CACHE.set("macro_signal", macro)
            return macro
        except Exception as e:
            log.error(f"Macro fetch failed: {e}")
            return None

    def _fetch_all_sources_for_ticker(
        self,
        ticker: str,
        df: Optional[pd.DataFrame] = None,
        macro = None,
    ) -> Dict[str, Any]:
        """
        Fetch all 9 sources for ONE ticker.
        Each source runs in its own mini-thread.
        Returns dict of source_name → result
        """
        results = {}

        def safe_fetch(source_name: str, fn: Callable) -> Any:
            cached = CACHE.get(f"{ticker}_{source_name}")
            if cached is not None:
                return cached
            try:
                RATE_LIMITER.wait(source_name)
                result = fn()
                CACHE.set(f"{ticker}_{source_name}", result)
                return result
            except Exception as e:
                log.debug(f"{source_name} failed for {ticker}: {e}")
                return None

        # Define all 9 fetch tasks
        tasks = {}

        if self.uw:
            tasks["unusual_whales"] = lambda: {
                "flow":     self.uw.fetch_flow(ticker, min_premium=50_000),
                "darkpool": self.uw.fetch_darkpool(ticker),
                "sentiment":self.uw.get_ticker_sentiment(ticker),
            }

        if self.poly:
            tasks["polygon"] = lambda: {
                "news":    self.poly.get_news(ticker, limit=5),
                "snapshot":self.poly.get_snapshot([ticker]),
                "iv":      self.poly.get_iv_data(ticker),
            }

        if self.benz:
            tasks["benzinga"] = lambda: {
                "news":    self.benz.get_news(ticker, limit=5),
                "ratings": self.benz.get_analyst_ratings(ticker),
            }

        if self.finv:
            tasks["finviz"] = lambda: {
                "technical": self.finv.get_technical(ticker, df),
            }

        if self.mc:
            tasks["market_chameleon"] = lambda: {
                "iv_rank": self.mc.get_iv_rank(ticker),
            }

        if self.tr:
            tasks["tipranks"] = lambda: {
                "consensus": self.tr.get_consensus(ticker),
            }

        if self.ew:
            tasks["earnings_whispers"] = lambda: {
                "earnings": self.ew.get_earnings(ticker),
            }

        if self.sec:
            tasks["sec_edgar"] = lambda: {
                "insider": self.sec.get_insider_trades(ticker),
            }

        # Run all source tasks in parallel (inner parallelism)
        with ThreadPoolExecutor(max_workers=9) as inner_pool:
            futures = {
                inner_pool.submit(safe_fetch, name, fn): name
                for name, fn in tasks.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as e:
                    log.debug(f"{name}/{ticker}: {e}")
                    results[name] = None

        # Macro shared across all tickers
        results["macro"] = macro

        return results

    def _score_bundle(
        self, ticker: str, raw: Dict[str, Any], macro
    ) -> "ScanResult":
        """Convert raw multi-source data into a scored ScanResult."""
        scores   = {}
        reasons  = []
        warnings = []

        # ── 1. Options flow score (35%) ───────────────────────────────────
        uw_data     = raw.get("unusual_whales") or {}
        sentiment   = uw_data.get("sentiment", {})
        flow_list   = uw_data.get("flow", [])
        dp_list     = uw_data.get("darkpool", [])
        flow_score  = float(sentiment.get("score", 50))
        call_count  = int(sentiment.get("calls", 0))
        put_count   = int(sentiment.get("puts", 0))
        sweep_count = sum(1 for f in flow_list if getattr(f, "is_sweep", False))
        dp_count    = len(dp_list)
        if sweep_count > 0:
            flow_score = min(100, flow_score + sweep_count * 5)
            reasons.append(f"{sweep_count} call sweep(s) detected")
        if dp_count > 0:
            flow_score = min(100, flow_score + dp_count * 3)
            reasons.append(f"{dp_count} dark pool block(s)")
        scores["options_flow"] = flow_score

        # Top flow signal
        top_flow = None
        if flow_list:
            top_flow = max(flow_list, key=lambda f: getattr(f, "premium", 0))

        # ── 2. Technical score (20%) ──────────────────────────────────────
        finv_data = raw.get("finviz") or {}
        tech      = finv_data.get("technical")
        tech_score = 50
        if tech:
            if getattr(tech, "above_200ema", False): tech_score += 15
            if getattr(tech, "above_50ema", False):  tech_score += 10
            if getattr(tech, "macd", "") == "BULLISH": tech_score += 15
            if getattr(tech, "rsi", 50) > 50:       tech_score += 5
            if getattr(tech, "volume_surge", False): tech_score += 10
            if getattr(tech, "pattern", "") == "BREAKOUT":
                tech_score += 15
                reasons.append(f"Breakout confirmed (RSI={getattr(tech,'rsi',0):.0f})")
        scores["technical"] = min(100, tech_score)

        # ── 3. News score (10%) ───────────────────────────────────────────
        benz_data  = raw.get("benzinga") or {}
        poly_data  = raw.get("polygon") or {}
        news_list  = (benz_data.get("news") or []) + (poly_data.get("news") or [])
        news_score = 50
        top_news   = None
        for n in news_list[:5]:
            sent = getattr(n, "sentiment", "NEUTRAL")
            if sent == "BULLISH":   news_score += 8
            elif sent == "BEARISH": news_score -= 8
            urgency = getattr(n, "urgency", 2)
            if urgency >= 4:
                reasons.append(f"High-urgency news: {getattr(n,'headline','')[:60]}")
        if news_list:
            top_news = news_list[0]
        scores["news"] = max(0, min(100, news_score))

        # ── 4. Analyst score (10%) ────────────────────────────────────────
        tr_data   = raw.get("tipranks") or {}
        analyst   = tr_data.get("consensus")
        benz_ratings = benz_data.get("ratings") or []
        analyst_score = 50
        if analyst:
            analyst_score = {
                "STRONG_BUY": 95, "BUY": 78,
                "HOLD": 50, "SELL": 25, "STRONG_SELL": 10
            }.get(getattr(analyst, "consensus", "HOLD"), 50)
            upside = getattr(analyst, "upside_pct", 0)
            if upside > 20:
                analyst_score = min(100, analyst_score + 10)
                reasons.append(f"Analyst: {getattr(analyst,'consensus','')} target ${getattr(analyst,'avg_target',0):.0f} (+{upside:.0f}%)")
            elif upside < -10:
                analyst_score = max(0, analyst_score - 10)
                warnings.append(f"Analyst target implies downside: {upside:.0f}%")
        # boost if recent upgrade
        for r in benz_ratings:
            if getattr(r, "recent_action", "") == "UPGRADE":
                analyst_score = min(100, analyst_score + 8)
                reasons.append(f"Recent analyst upgrade")
                break
        scores["analyst"] = analyst_score

        # ── 5. Earnings score (15%) ───────────────────────────────────────
        ew_data  = raw.get("earnings_whispers") or {}
        earnings = ew_data.get("earnings")
        earn_score = 50
        if earnings:
            days = getattr(earnings, "days_until", 99)
            if 3 <= days <= 14:
                earn_score += 25
                whisper = getattr(earnings, "whisper_eps", 0)
                consensus = getattr(earnings, "consensus_eps", 0)
                beat_pct = ((whisper - consensus) / max(consensus, 0.01)) * 100
                reasons.append(f"Earnings in {days}d — whisper ${whisper:.2f} ({beat_pct:+.0f}% vs consensus)")
            elif 15 <= days <= 30:
                earn_score += 12
            surprise = getattr(earnings, "surprise_history", 0)
            if surprise > 10:
                earn_score += 15
                reasons.append(f"Serial earnings beater (+{surprise:.0f}% avg surprise)")
            iv_rank = getattr(earnings, "iv_rank", 50)
            if iv_rank > 65:
                earn_score += 10
                reasons.append(f"Elevated IV rank ({iv_rank:.0f}) — market expects big move")
        scores["earnings"] = min(100, earn_score)

        # ── 6. IV / Options pricing score (bonus from Market Chameleon) ───
        mc_data  = raw.get("market_chameleon") or {}
        iv_data  = mc_data.get("iv_rank") or {}
        iv_rank  = float(iv_data.get("iv_rank", 50))
        poly_iv  = (poly_data.get("iv") or {}).get("iv_rank", 50)
        combined_iv_rank = (iv_rank + float(poly_iv)) / 2
        # Low IV rank = cheap options = good entry for buyers
        if combined_iv_rank < 30:
            reasons.append(f"Low IV rank ({combined_iv_rank:.0f}) — cheap options entry")

        # ── 7. Macro score (10%) ──────────────────────────────────────────
        macro_score = 50
        if macro:
            macro_score = {
                "RISK_ON":  85,
                "NEUTRAL":  55,
                "RISK_OFF": 28,
                "CRASH":    8,
            }.get(getattr(macro, "market_regime", "NEUTRAL"), 50)
            regime = getattr(macro, "market_regime", "NEUTRAL")
            if regime in ("RISK_OFF", "CRASH"):
                warnings.append(f"⚠ Macro: {regime} (VIX={getattr(macro,'vix',0):.1f})")
        scores["macro"] = macro_score

        # ── 8. Insider score (bonus) ──────────────────────────────────────
        sec_data = raw.get("sec_edgar") or {}
        insiders = sec_data.get("insider") or []
        insider_bonus = 0
        for ins in insiders:
            if getattr(ins, "action", "") == "BUY":
                insider_bonus += 3
                reasons.append(f"Insider buying: {getattr(ins,'filer','')}")
                break

        # ── WEIGHTED CONVICTION SCORE ─────────────────────────────────────
        weights = {
            "options_flow": 0.35,
            "technical":    0.20,
            "news":         0.10,
            "analyst":      0.10,
            "earnings":     0.15,
            "macro":        0.10,
        }
        conviction = sum(
            scores.get(k, 50) * w for k, w in weights.items()
        ) + insider_bonus
        conviction = max(0, min(100, conviction))

        # Direction
        bull = sum(1 for s in scores.values() if s > 62)
        bear = sum(1 for s in scores.values() if s < 38)
        direction = "LONG" if bull >= 3 else "SHORT" if bear >= 3 else "NEUTRAL"

        # Current price
        price = 0.0
        snap  = poly_data.get("snapshot", {})
        if snap:
            price = float(snap.get(ticker, 0))

        return ScanResult(
            ticker=ticker,
            sector=TICKER_TO_SECTOR.get(ticker, "Unknown"),
            conviction=round(conviction, 1),
            direction=direction,
            price=price,
            scores=scores,
            signals_count=bull + bear,
            bull_signals=bull,
            bear_signals=bear,
            top_reasons=reasons[:4],
            warnings=warnings,
            top_flow=top_flow,
            top_news=top_news,
            analyst=analyst,
            technical=tech,
            earnings=earnings,
            macro=macro,
            iv_rank=round(combined_iv_rank, 1),
            sweep_count=sweep_count,
            darkpool_count=dp_count,
        )

    def fetch_ticker(
        self,
        ticker: str,
        df: Optional[pd.DataFrame] = None,
        macro=None,
    ) -> Optional["ScanResult"]:
        """Fetch all sources for a single ticker and return ScanResult."""
        try:
            raw    = self._fetch_all_sources_for_ticker(ticker, df, macro)
            result = self._score_bundle(ticker, raw, macro)
            with self._progress_lock:
                self._completed += 1
            return result
        except Exception as e:
            log.error(f"Failed to bundle {ticker}: {e}")
            return None

    def scan_all(
        self,
        tickers: List[str] = None,
        data_map: Dict[str, pd.DataFrame] = None,
        min_conviction: float = 0,
        on_progress: Callable = None,
    ) -> List["ScanResult"]:
        """
        Scan ALL tickers with ALL 9 sources in parallel.
        
        Args:
            tickers:        list of tickers (default: ALL_UNIVERSE_TICKERS)
            data_map:       pre-fetched OHLCV DataFrames per ticker
            min_conviction: filter results below this score
            on_progress:    callback(completed, total) for progress tracking
        
        Returns:
            List[ScanResult] sorted by conviction descending
        """
        tickers  = tickers or ALL_UNIVERSE_TICKERS
        data_map = data_map or {}
        total    = len(tickers)
        self._completed = 0

        log.info(f"🔍 Starting full universe scan: {total} tickers | "
                 f"{self.max_workers} workers | all 9 sources")

        # Fetch macro once (shared across all tickers)
        log.info("Fetching macro signal (VIX, put/call, credit spread)...")
        macro = self._fetch_macro_once()

        results  = []
        errors   = []
        start_ts = time.time()

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_map = {
                pool.submit(
                    self.fetch_ticker,
                    ticker,
                    data_map.get(ticker),
                    macro,
                ): ticker
                for ticker in tickers
            }

            for future in as_completed(future_map):
                ticker = future_map[future]
                try:
                    result = future.result(timeout=30)
                    if result:
                        results.append(result)
                        if on_progress:
                            on_progress(self._completed, total)
                        # Live progress log every 10 tickers
                        if self._completed % 10 == 0:
                            elapsed = time.time() - start_ts
                            rate    = self._completed / max(elapsed, 1)
                            eta     = (total - self._completed) / max(rate, 0.1)
                            log.info(
                                f"Progress: {self._completed}/{total} "
                                f"| {rate:.1f}/s | ETA {eta:.0f}s"
                            )
                except Exception as e:
                    errors.append(ticker)
                    log.debug(f"Future error {ticker}: {e}")

        elapsed = time.time() - start_ts
        log.info(
            f"✅ Scan complete: {len(results)}/{total} tickers in {elapsed:.1f}s "
            f"| {len(errors)} errors"
        )

        # Sort by conviction
        results.sort(key=lambda r: r.conviction, reverse=True)

        # Filter
        if min_conviction > 0:
            results = [r for r in results if r.conviction >= min_conviction]
            log.info(f"Filtered to {len(results)} tickers above {min_conviction} conviction")

        return results


# ══════════════════════════════════════════════════════════════════════════
# SCAN RESULT — richer than ConvictionBundle, includes all raw scores
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class ScanResult:
    ticker:          str
    sector:          str
    conviction:      float
    direction:       str       # LONG / SHORT / NEUTRAL
    price:           float
    scores:          Dict[str, float]
    signals_count:   int
    bull_signals:    int
    bear_signals:    int
    top_reasons:     List[str]
    warnings:        List[str]
    top_flow:        Any = None
    top_news:        Any = None
    analyst:         Any = None
    technical:       Any = None
    earnings:        Any = None
    macro:           Any = None
    iv_rank:         float = 50.0
    sweep_count:     int = 0
    darkpool_count:  int = 0
    timestamp:       str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        """Flat dict for CSV/JSON export."""
        d = {
            "ticker":           self.ticker,
            "sector":           self.sector,
            "conviction":       self.conviction,
            "direction":        self.direction,
            "price":            self.price,
            "bull_signals":     self.bull_signals,
            "bear_signals":     self.bear_signals,
            "iv_rank":          self.iv_rank,
            "sweep_count":      self.sweep_count,
            "darkpool_count":   self.darkpool_count,
            "top_reason":       self.top_reasons[0] if self.top_reasons else "",
            "warnings":         " | ".join(self.warnings),
            "timestamp":        self.timestamp,
        }
        for k, v in self.scores.items():
            d[f"score_{k}"] = round(v, 1)
        if self.analyst:
            d["analyst_consensus"] = getattr(self.analyst, "consensus", "")
            d["analyst_target"]    = getattr(self.analyst, "avg_target", 0)
            d["analyst_upside"]    = getattr(self.analyst, "upside_pct", 0)
        if self.earnings:
            d["earnings_date"]  = getattr(self.earnings, "earnings_date", "")
            d["earnings_days"]  = getattr(self.earnings, "days_until", 0)
            d["whisper_eps"]    = getattr(self.earnings, "whisper_eps", 0)
        if self.technical:
            d["rsi"]         = getattr(self.technical, "rsi", 0)
            d["macd"]        = getattr(self.technical, "macd", "")
            d["pattern"]     = getattr(self.technical, "pattern", "")
            d["vol_surge"]   = getattr(self.technical, "volume_surge", False)
        return d


# ══════════════════════════════════════════════════════════════════════════
# UNIVERSE SCANNER — main class the agent uses
# ══════════════════════════════════════════════════════════════════════════
class UniverseScanner:
    """
    The agent calls this. One call → all stocks → all sources → ranked list.
    
    Usage:
        scanner = UniverseScanner()
        
        # Scan everything
        results = scanner.scan()
        
        # Scan one sector
        results = scanner.scan(sector="AI_Compute")
        
        # Scan specific tickers
        results = scanner.scan(tickers=["NVDA", "MU", "ORCL"])
        
        # Top N only
        top10 = scanner.top(n=10)
        
        # Export to CSV
        scanner.export_csv("scan_results.csv")
    """

    def __init__(self, max_workers: int = 20, min_conviction: float = 60):
        self.fetcher        = ParallelBundleFetcher(max_workers=max_workers)
        self.min_conviction = min_conviction
        self._last_results: List[ScanResult] = []
        self._last_scan_ts: Optional[datetime] = None

    def scan(
        self,
        tickers:        List[str] = None,
        sector:         str = None,
        min_conviction: float = None,
        data_map:       Dict[str, pd.DataFrame] = None,
    ) -> List[ScanResult]:
        """
        Main scan entry point. Returns ALL results sorted by conviction.
        """
        # Resolve ticker list
        if tickers:
            scan_tickers = tickers
        elif sector:
            scan_tickers = UNIVERSE.get(sector, ALL_UNIVERSE_TICKERS)
            log.info(f"Scanning sector: {sector} ({len(scan_tickers)} tickers)")
        else:
            scan_tickers = ALL_UNIVERSE_TICKERS
            log.info(f"Scanning FULL universe: {len(scan_tickers)} tickers")

        threshold = min_conviction or self.min_conviction

        results = self.fetcher.scan_all(
            tickers=scan_tickers,
            data_map=data_map,
            min_conviction=threshold,
        )

        self._last_results = results
        self._last_scan_ts = datetime.now()
        return results

    def top(self, n: int = 10, direction: str = None) -> List[ScanResult]:
        """Return top N results, optionally filtered by direction."""
        results = self._last_results
        if direction:
            results = [r for r in results if r.direction == direction]
        return results[:n]

    def by_sector(self) -> Dict[str, List[ScanResult]]:
        """Group last scan results by sector."""
        groups: Dict[str, List[ScanResult]] = {}
        for r in self._last_results:
            groups.setdefault(r.sector, []).append(r)
        return groups

    def sector_heat(self) -> Dict[str, float]:
        """Average conviction per sector — shows where momentum is."""
        groups = self.by_sector()
        return {
            sector: round(sum(r.conviction for r in results) / len(results), 1)
            for sector, results in groups.items()
            if results
        }

    def alerts(self) -> List[ScanResult]:
        """Return only tickers with sweep activity or high-urgency news."""
        return [
            r for r in self._last_results
            if r.sweep_count > 0 or r.darkpool_count > 0
        ]

    def earnings_plays(self, days_ahead: int = 14) -> List[ScanResult]:
        """Return tickers with earnings within N days."""
        plays = []
        for r in self._last_results:
            if r.earnings and getattr(r.earnings, "days_until", 99) <= days_ahead:
                plays.append(r)
        return sorted(plays, key=lambda x: getattr(x.earnings, "days_until", 99))

    def export_csv(self, path: str = "ktrade_scan.csv") -> str:
        """Export all results to CSV."""
        if not self._last_results:
            log.warning("No results to export — run scan() first")
            return ""
        rows = [r.to_dict() for r in self._last_results]
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        log.info(f"Exported {len(rows)} results → {path}")
        return path

    def export_json(self, path: str = "ktrade_scan.json") -> str:
        """Export all results to JSON."""
        if not self._last_results:
            return ""
        rows = [r.to_dict() for r in self._last_results]
        with open(path, "w") as f:
            json.dump({
                "scan_time":  self._last_scan_ts.isoformat() if self._last_scan_ts else "",
                "total":      len(rows),
                "universe":   len(ALL_UNIVERSE_TICKERS),
                "results":    rows,
            }, f, indent=2, default=str)
        log.info(f"Exported {len(rows)} results → {path}")
        return path

    def print_leaderboard(self, top_n: int = 20):
        """Print a clean ranked leaderboard to console."""
        results = self._last_results[:top_n]
        scan_time = self._last_scan_ts.strftime("%H:%M:%S") if self._last_scan_ts else "?"

        print(f"\n{'='*80}")
        print(f"  KTrade PRO — Universe Scan Results  |  {scan_time}")
        print(f"  Scanned: {len(ALL_UNIVERSE_TICKERS)} tickers | "
              f"Showing top {min(top_n, len(results))}")
        print(f"{'='*80}")
        print(f"  {'#':<3} {'Ticker':<7} {'Sector':<16} {'CV':>4} {'Dir':<8} "
              f"{'Price':>7} {'RSI':>5} {'IV%':>5} {'Sweep':>5} {'Top Reason'}")
        print(f"  {'-'*76}")

        for i, r in enumerate(results, 1):
            rsi  = getattr(r.technical, "rsi", 0) if r.technical else 0
            earns = f"📅{getattr(r.earnings,'days_until',0)}d" if r.earnings else ""
            sweep = f"🔥x{r.sweep_count}" if r.sweep_count > 0 else \
                    f"🌑x{r.darkpool_count}" if r.darkpool_count > 0 else ""
            reason = r.top_reasons[0][:38] if r.top_reasons else ""
            warn   = "⚠" if r.warnings else ""
            dir_icon = "🟢" if r.direction=="LONG" else "🔴" if r.direction=="SHORT" else "⚪"

            print(
                f"  {i:<3} {r.ticker:<7} {r.sector[:15]:<16} "
                f"{r.conviction:>4.0f} {dir_icon}{r.direction:<7} "
                f"${r.price:>6.1f} {rsi:>5.0f} {r.iv_rank:>5.0f} "
                f"{sweep:>5} {reason}{warn}"
            )
            if r.earnings:
                days = getattr(r.earnings, "days_until", 0)
                date = getattr(r.earnings, "earnings_date", "")
                print(f"       📅 Earnings {date} ({days}d) — "
                      f"whisper ${getattr(r.earnings,'whisper_eps',0):.2f}")

        print(f"{'='*80}\n")

        # Sector heat
        heat = self.sector_heat()
        if heat:
            print("  Sector Heat Map:")
            for sector, avg in sorted(heat.items(), key=lambda x: -x[1])[:8]:
                bar = "█" * int(avg / 10)
                print(f"    {sector:<18} {avg:>5.1f}  {bar}")
            print()

        # Alerts
        alerts = self.alerts()
        if alerts:
            print(f"  🚨 Flow Alerts ({len(alerts)} tickers with sweeps/darkpool):")
            for a in alerts[:5]:
                print(f"    {a.ticker}: {a.sweep_count} sweeps, "
                      f"{a.darkpool_count} darkpool blocks | CV={a.conviction:.0f}")
            print()

        # Earnings plays
        plays = self.earnings_plays(days_ahead=14)
        if plays:
            print(f"  📅 Earnings Plays (next 14 days):")
            for p in plays[:5]:
                e = p.earnings
                print(f"    {p.ticker}: {getattr(e,'earnings_date','')} "
                      f"({getattr(e,'days_until',0)}d) | "
                      f"CV={p.conviction:.0f} | IV rank={p.iv_rank:.0f}")
            print()


# ══════════════════════════════════════════════════════════════════════════
# CLI — run directly from command line
# ══════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="KTrade PRO — Full Universe Scanner (all stocks, all sources)"
    )
    parser.add_argument("--top",       type=int,   default=20,
                        help="Show top N results (default: 20)")
    parser.add_argument("--sector",    type=str,   default=None,
                        help=f"Scan one sector: {list(UNIVERSE.keys())}")
    parser.add_argument("--ticker",    nargs="+",  default=None,
                        help="Scan specific tickers: --ticker NVDA MU ORCL")
    parser.add_argument("--min-score", type=float, default=60,
                        help="Minimum conviction score (default: 60)")
    parser.add_argument("--workers",   type=int,   default=20,
                        help="Parallel workers (default: 20)")
    parser.add_argument("--export",    action="store_true",
                        help="Export results to CSV + JSON")
    parser.add_argument("--watch",     action="store_true",
                        help="Continuous mode: rescan every 5 minutes")
    parser.add_argument("--longs",     action="store_true",
                        help="Show only LONG signals")
    parser.add_argument("--shorts",    action="store_true",
                        help="Show only SHORT signals")
    parser.add_argument("--sweeps",    action="store_true",
                        help="Show only tickers with sweep/darkpool activity")
    parser.add_argument("--earnings",  action="store_true",
                        help="Show only upcoming earnings plays")
    args = parser.parse_args()

    print(f"\n{'='*80}")
    print(f"  KTrade PRO — Full Universe Scanner")
    print(f"  Universe: {len(ALL_UNIVERSE_TICKERS)} tickers | "
          f"{len(UNIVERSE)} sectors | 9 signal sources")
    print(f"  Workers:  {args.workers} parallel threads")
    print(f"  Sources:  Unusual Whales {'✅' if UNUSUAL_WHALES_KEY else '⚠ demo'} | "
          f"Polygon {'✅' if POLYGON_KEY else '⚠ demo'} | "
          f"Benzinga {'✅' if BENZINGA_KEY else '⚠ demo'} | "
          f"Free sources: Finviz, MC, EW, CBOE, EDGAR ✅")
    print(f"{'='*80}\n")

    scanner = UniverseScanner(
        max_workers=args.workers,
        min_conviction=args.min_score,
    )

    def run_scan():
        results = scanner.scan(
            tickers=args.ticker,
            sector=args.sector,
            min_conviction=args.min_score,
        )

        # Apply filters
        display = results
        if args.longs:
            display = [r for r in results if r.direction == "LONG"]
        elif args.shorts:
            display = [r for r in results if r.direction == "SHORT"]
        if args.sweeps:
            display = [r for r in display if r.sweep_count > 0 or r.darkpool_count > 0]
        if args.earnings:
            display = scanner.earnings_plays(days_ahead=14)

        scanner._last_results = display
        scanner.print_leaderboard(top_n=args.top)

        if args.export:
            csv_path  = scanner.export_csv(
                f"ktrade_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
            json_path = scanner.export_json(
                f"ktrade_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
            print(f"  Exported: {csv_path}")
            print(f"  Exported: {json_path}")

        return results

    if args.watch:
        print("  Live watch mode — rescanning every 5 minutes. Ctrl+C to stop.\n")
        try:
            while True:
                run_scan()
                print(f"  Next scan in 5 minutes... ({datetime.now().strftime('%H:%M:%S')})\n")
                time.sleep(300)
        except KeyboardInterrupt:
            print("\n  Watch mode stopped.")
    else:
        run_scan()


if __name__ == "__main__":
    main()
