"""
KTrade PRO â€” Unified Trading Agent
====================================
Version: 9.0
Date:    2026-06-15
Author:  iT LLC

WHAT THIS FILE IS:
  Single unified agent that merges ALL previously built modules:

  FROM trading_agent_master.py (v8.1 â†’ v8.3):
    âœ… Config, DataFeed, RiskManager, RegimeFilter, CrashGuard
    âœ… Broker, TradeJournal, Position
    âœ… TrendFilterStrategy, MomentumStrategy, BreakoutStrategy
    âœ… UniverseLoader, Scanner, SectorRotation, Backtester, WalkForward
    âœ… v8.3 additions: signal_is_fresh, cost_ok, spread_ok, structural_stop
                      trailing_stop, relative_strength_score, CrashGuard

  FROM claude_macd_ema_strategy (Jun 10):
    âœ… MACDEMAStrategy â€” MACD crossover + 200 EMA confluence
    âœ… MACDEMAConfluenceStrategy â€” full price action confirmation

  FROM claude_orb_strategy (Jun 10):
    âœ… ORBRangeBuilder â€” builds opening range from 15m/5m candles
    âœ… ORBSignalDetector â€” detects breakout on 5m
    âœ… ORBEntryFinder â€” precise entry on 1m
    âœ… ORBStrategy â€” adapter for Scanner interface

  FROM claude_heartbeat (Jun 10):
    âœ… ConvictionScorer â€” scores ~200 tickers across 6 weighted components
    âœ… HeartbeatEngine â€” market-phase-aware scan frequency
    âœ… DailyLossGuard â€” kill switch
    âœ… PositionMonitor â€” auto stop/target exits at 1.5R

  FROM claude_ceo_orchestrator_v2 (Jun 15):
    âœ… CEO architecture: Research â†’ Strategy â†’ Risk â†’ Execution â†’ Cost agents
    âœ… AgentMessage bus
    âœ… CostOptimizerAgent â€” dynamic API call budgeting
    âœ… --ask CLI interface for natural language queries

  FROM KTrade risk engine (Jun 15, THIS SESSION):
    âœ… Duplicate order prevention (60s window)
    âœ… Same-ticker cooldown (5 min post-fill)
    âœ… ATR-based position sizing
    âœ… Kelly Criterion (25% fractional)
    âœ… VIX circuit breakers
    âœ… Flash crash detection
    âœ… Broker-side bracket orders
    âœ… Short/hedge tracking

  FROM crash detection agent (Jun 5):
    âœ… VIX spike detector
    âœ… Put/call ratio monitor
    âœ… Sector rotation signal
    âœ… SPY/QQQ put recommendation engine

USAGE:
  python ktrade_agent.py                    # autonomous loop
  python ktrade_agent.py --once             # single cycle
  python ktrade_agent.py --score-only       # safe: score tickers, no trades
  python ktrade_agent.py --validate         # backtest all strategies
  python ktrade_agent.py --ask "Top play?"  # natural language query
  python ktrade_agent.py --crash-check      # run crash detection only

.env required:
  ALPACA_KEY=PKxxxxxxxxxxxxxxxxxx
  ALPACA_SECRET=xxxxxxxxxxxxxxxxxx
  ACCOUNT_VALUE=100000
  RISK_PER_TRADE=0.005
  LIVE_TRADING=false
  MIN_CONVICTION_SCORE=60
  MAX_POSITIONS_PER_HEARTBEAT=3
  DAILY_LOSS_LIMIT_PCT=0.02
  MAX_DAILY_API_CALLS=500
  COST_BUDGET_USD=5.0
  MAX_SIGNAL_AGE_DAYS=2
  COMMISSION_PER_TRADE=0
  MAX_COST_FRAC=0.005
  USE_CRASH_GUARD=true
  SECTOR_ROTATION=true
"""

from __future__ import annotations

import os, sys, time, math, logging, argparse, json, uuid
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date, timezone
from enum import Enum
from typing import Optional, List, Dict, Any, Tuple

import numpy as np
import pandas as pd

# v10.3: hard bad-tick / decimal-shift gate, shared singleton
try:
    from data.price_sanity import PRICE_GUARD
except Exception:  # pragma: no cover - score-only contexts may not have it on path
    PRICE_GUARD = None

# v10.7: data hygiene, partial-fill accounting, emergency + persistence helpers
try:
    from data.schema_validation import normalize_ohlcv_frame, drop_unclosed_last_bar
except Exception:
    normalize_ohlcv_frame = None
    drop_unclosed_last_bar = None
try:
    from risk.position_fills import apply_fill_to_position
except Exception:
    apply_fill_to_position = None
try:
    from risk.emergency import EmergencyController
except Exception:
    EmergencyController = None
try:
    from risk.state_store import RiskStateStore
except Exception:
    RiskStateStore = None
try:
    from data.earnings_calendar import EarningsCalendar
except Exception:
    EarningsCalendar = None

# v12.2: pre-load env before logging so KTRADE_LOG_DIR/KTRADE_APP_DATA_DIR work
try:
    from ktrade_runtime.paths import load_ktrade_env
    load_ktrade_env()
except Exception:
    pass

# â”€â”€ Logging â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s â€” %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.getenv("KTRADE_AGENT_LOG", str((Path(os.getenv("KTRADE_LOG_DIR", ".")) / "ktrade_agent.log"))), mode="a"),
    ],
)
log = logging.getLogger("KTrade")
__version__ = "13.4"
__updated__ = "2026-06-27"

# Optional: load env from project or KTrade app-data runtime directory
project_dir = Path(__file__).resolve().parent.parent
try:
    from ktrade_runtime.paths import load_ktrade_env, logs_dir
    load_ktrade_env()
except Exception:
    try:
        from dotenv import dotenv_values
        for env_key, env_value in dotenv_values(
            project_dir / ".env", encoding="utf-8-sig"
        ).items():
            if env_value is not None:
                os.environ[env_key] = env_value
    except Exception:
        pass

# â”€â”€ Try importing existing master (if available locally) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    from trading_agent_master import (
        Config, DataFeed, CachedDataFeed, LiveDataFeed, RiskManager,
        RegimeFilter, CrashGuard, Broker, TradeJournal, Position,
        TrendFilterStrategy, MomentumStrategy, BreakoutStrategy,
        UniverseLoader, Scanner, SectorRotation, Backtester, WalkForward,
        ALL_IDEAS, WEAKNESS_IDEAS, idea_symbols, RiskTier, Candidate,
        _atr, spread_ok, structural_stop, time_stop_hit, trailing_stop,
        signal_is_fresh, cost_fraction, cost_ok, relative_strength_score,
    )
    log.info("âœ“ trading_agent_master (base trading logic) imported")
    MASTER_AVAILABLE = True
except ImportError:
    log.warning("trading_agent_master not found â€” running standalone mode")
    MASTER_AVAILABLE = False


# ===========================================================================
# SECTION 1 â€” CONFIGURATION
# ===========================================================================
@dataclass
class KTradeConfig:
    """Unified config for all KTrade agent modules."""
    # Account
    account_value:          float = float(os.getenv("ACCOUNT_VALUE", 100_000))
    risk_per_trade:         float = float(os.getenv("RISK_PER_TRADE", 0.005))
    live_trading:           bool  = os.getenv("LIVE_TRADING", "false").lower() == "true"

    # Alpaca
    alpaca_key:             str   = os.getenv("ALPACA_KEY", "")
    alpaca_secret:          str   = os.getenv("ALPACA_SECRET", "")
    alpaca_base:            str   = "https://paper-api.alpaca.markets"  # paper default
    alpaca_data:            str   = "https://data.alpaca.markets"

    # Strategy gates
    min_conviction_score:   int   = int(os.getenv("MIN_CONVICTION_SCORE", 75))  # v10.8: match RISK_RULES.md (was 60)
    max_positions:          int   = int(os.getenv("MAX_POSITIONS_PER_HEARTBEAT", 3))
    max_signal_age_days:    int   = int(os.getenv("MAX_SIGNAL_AGE_DAYS", 2))

    # Risk limits
    daily_loss_limit_pct:   float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", 0.02))
    max_daily_drawdown_pct: float = 3.0
    max_daily_loss_dollars: float = 3000.0
    max_open_positions:     int   = 10
    max_position_size_pct:  float = 10.0
    max_trade_dollar_risk:  float = 1000.0

    # Duplicate / cooldown (KTrade risk engine fixes)
    duplicate_window_sec:   int   = 60
    ticker_cooldown_sec:    int   = 300
    max_ticker_per_day:     int   = 3

    # VIX circuit breakers
    vix_elevated:           float = 25.0
    vix_risk_off:           float = 30.0
    vix_close_all:          float = 50.0
    vix_auto_hedge:         float = 28.0
    flash_crash_drop_pct:   float = 2.5

    # Position sizing
    atr_risk_multiplier:    float = 1.5
    default_stop_pct:       float = 2.0
    default_target_pct:     float = 4.0
    kelly_win_rate:         float = 0.55
    kelly_avg_win:          float = 1.8
    kelly_fraction:         float = 0.25

    # Cost management (v8.3)
    max_daily_api_calls:    int   = int(os.getenv("MAX_DAILY_API_CALLS", 500))
    cost_budget_usd:        float = float(os.getenv("COST_BUDGET_USD", 5.0))
    commission_per_trade:   float = float(os.getenv("COMMISSION_PER_TRADE", 0))
    max_cost_frac:          float = float(os.getenv("MAX_COST_FRAC", 0.005))

    # Features
    use_crash_guard:        bool  = os.getenv("USE_CRASH_GUARD", "true").lower() == "true"
    sector_rotation:        bool  = os.getenv("SECTOR_ROTATION", "true").lower() == "true"
    allow_shorts:           bool  = True
    max_short_exposure_pct: float = 15.0
    # v10.5: correlation / concentration cap (protects against piling into
    # one correlated basket, e.g. AI/semis, on a sector-rotation down day).
    max_sector_exposure_pct:  float = float(os.getenv("MAX_SECTOR_EXPOSURE_PCT", 30.0))
    max_positions_per_sector: int   = int(os.getenv("MAX_POSITIONS_PER_SECTOR", 3))
    # v10.6: refuse a first BUY when no trusted price reference exists, so a
    # first-time ticker cannot pass a decimal-shift price unchecked.
    require_price_reference:  bool  = os.getenv("KTRADE_REQUIRE_PRICE_REF", "true").lower() == "true"
    # v10.9: earnings-event awareness
    earnings_blackout_days:   int   = int(os.getenv("KTRADE_EARNINGS_BLACKOUT_DAYS", 3))   # block new BUY within N days of earnings
    earnings_exit_ahead:      bool  = os.getenv("KTRADE_EARNINGS_EXIT_AHEAD", "true").lower() == "true"
    earnings_exit_days:       int   = int(os.getenv("KTRADE_EARNINGS_EXIT_DAYS", 2))        # exit holdings within N days of earnings

CFG = KTradeConfig()


# ===========================================================================
# SECTION 2 â€” TICKER UNIVERSE (~200 tickers, from heartbeat module)
# ===========================================================================
# ===========================================================================
# v10.5 - SECTOR / CORRELATION MAP (for concentration limits)
# Tickers grouped into correlated baskets. Names not listed map to "OTHER"
# and are exempt from the sector cap (sized individually).
# ===========================================================================
_SECTOR_GROUPS = {
    "AI_SEMI": [
        "NVDA","AMD","INTC","AVGO","QCOM","MRVL","AMAT","LRCX","KLAC","ASML",
        "MU","WDC","STX","ON","SWKS","QRVO","MPWR","TXN","ADI","TSM","SMCI",
        "ARM","CRDO","RMBS","MOD","NVTS","DELL","HPE","ANET","SNDK",
    ],
    "AI_CLOUD_SW": [
        "MSFT","GOOGL","GOOG","AMZN","META","ORCL","CRM","SNOW","MDB","NET",
        "DDOG","NOW","ADBE","INTU","PANW","ZS","CRWD","S","PLTR","AI","BBAI",
        "SOUN","PATH","NBIS","CRWV","ASTERA","NEBIUS",
    ],
    "QUANTUM_SPACE": [
        "IONQ","RGTI","QBTS","SPCE","SPCX","RKLB","ASTS","LUNR","RDW","MNTS",
        "BWXT","LEU","KTOS","IRDM","DXYZ",
    ],
    "EV_AUTO": ["TSLA","RIVN","LCID","NIO","XPEV","LI","F","GM","TM","STLA"],
    "POWER_ENERGY": [
        "CEG","VST","GEV","ETN","PWR","VRT","NRG","ENPH","SEDG","FSLR",
        "BE","PLUG","BLDP","NEE","TLN",
    ],
    "CRYPTO": ["COIN","MSTR","IREN","MARA","RIOT","BTC-USD","ETH-USD"],
    "FINANCIALS": ["JPM","GS","MS","BAC","C","WFC","BX","KKR","APO","ARES"],
    "BIOTECH_HEALTH": ["MRNA","BNTX","NVAX","TDOC","DOCS","ACCD","HIMS","WELL","DVA","HCA"],
    "INDEX_ETF": ["SPY","QQQ","IWM","DIA","TQQQ","SOXX","XLK","XLF","XLE","XLV","XLI","QTUM","IDGT","XAR"],
    "SAFE_HAVEN": ["GLD","TLT"],
}
SECTOR_MAP = {t.upper(): grp for grp, names in _SECTOR_GROUPS.items() for t in names}

def sector_of(ticker: str) -> str:
    return SECTOR_MAP.get((ticker or "").upper(), "OTHER")


BROAD_UNIVERSE = [
    # AI Compute
    "NVDA","AMD","INTC","AVGO","QCOM","MRVL","AMAT","LRCX","KLAC","ASML",
    # AI Cloud / Infrastructure
    "MSFT","GOOGL","AMZN","META","ORCL","CRM","SNOW","MDB","NET","DDOG",
    # AI Apps / Software
    "PLTR","ABNB","UBER","LYFT","COIN","PATH","AI","BBAI","SOUN","IREN",
    # Semiconductors
    "TSM","MU","WDC","STX","ON","SWKS","QRVO","MPWR","TXN","ADI",
    # EV / Robotics
    "TSLA","RIVN","LCID","NIO","XPEV","LI","F","GM","TM","STLA",
    # Large Cap Tech
    "AAPL","NFLX","CRM","NOW","ADBE","INTU","PANW","ZS","CRWD","S",
    # Quantum / Space
    "IONQ","RGTI","QBTS","SPCE","RKLB","ASTS","LUNR","RDW","MNTS","BWXT",
    # Biotech / Health
    "MRNA","BNTX","NVAX","TDOC","DOCS","ACCD","HIMS","WELL","DVA","HCA",
    # Energy / Clean
    "ENPH","SEDG","FSLR","BE","PLUG","BLDP","NEE","CEG","VST","NRG",
    # Financials
    "JPM","GS","MS","BAC","C","WFC","BX","KKR","APO","ARES",
    # ETFs (used for macro signals)
    "SPY","QQQ","IWM","DIA","XLK","XLF","XLE","XLV","XLI","GLD",
    # Krishna Sumanth watchlist additions
    "CRDO","ARM","MSTR","SMCI","DELL","HPE","VMW","ANET","JNPR","CSCO",
]



# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# APPROVED PARAMS LOADER â€” reads ktrade_vectorbt.py output
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
import json as _json
from pathlib import Path as _Path

_PARAMS_FILE = _Path(__file__).parent.parent / "data" / "ktrade_approved_params.json"
_INTRADAY_PARAMS_FILE = _Path(__file__).parent.parent / "data" / "ktrade_intraday_approved_params.json"

def load_approved_params(path: _Path = _PARAMS_FILE) -> dict:
    """
    Load VectorBT-approved strategy parameters.
    Returns dict: {ticker: {strategy: {params, sharpe, win_rate, ...}}}.
    Unapproved strategies are ignored so live logic cannot accidentally use them.
    """
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            approved = {
                str(t).upper(): {
                    str(s).upper(): v
                    for s, v in (strats or {}).items()
                    if isinstance(v, dict) and v.get("approved")
                }
                for t, strats in (data or {}).items()
            }
            approved = {t: s for t, s in approved.items() if s}
            n = sum(len(v) for v in approved.values())
            log.info(f"Loaded {n} VectorBT-approved strategy params from {path}")
            return approved
        except Exception as e:
            log.warning(f"Could not load approved params from {path}: {e}")
    log.info(f"No approved params found at {path}; using safe defaults")
    return {}

def get_ticker_params(approved: dict, ticker: str, strategy: str) -> dict:
    """Get approved params for ticker+strategy, case-insensitive, else {}."""
    strats = approved.get((ticker or "").upper(), {})
    key = (strategy or "").upper()
    if key in strats:
        return dict(strats[key].get("params", {}) or {})
    aliases = {
        "MACD_EMA": "MACD",
        "MACD_EMA_CONFLUENCE": "CONVICTION",
        "MOMENTUM": "MOMENTUM",
        "TREND": "EMA",
        "EMA": "EMA",
        "ORB": "ORB_VWAP",
    }
    alias = aliases.get(key)
    if alias and alias in strats:
        return dict(strats[alias].get("params", {}) or {})
    return {}

APPROVED_PARAMS = load_approved_params(_PARAMS_FILE)
APPROVED_INTRADAY_PARAMS = load_approved_params(_INTRADAY_PARAMS_FILE)



# ===========================================================================
# SECTION 3 â€” MARKET STATE (shared across all agents)
# ===========================================================================
@dataclass
class MarketState:
    vix:                float = 18.0
    spy_price:          float = 550.0
    spy_prev_10m:       float = 550.0
    put_call_ratio:     float = 0.8
    advance_decline:    float = 1.0   # > 1 = more advancers
    flash_crash_active: bool  = False
    crash_risk_score:   float = 0.0   # 0-100, from crash detection agent
    market_open:        bool  = True
    phase:              str   = "REGULAR"  # PRE / REGULAR / POWER_HOUR / AFTER
    last_updated:       str   = ""
    vix_updated_at:     str   = ""   # v12.8: when VIX was last set from a real feed

MARKET = MarketState()


def _vix_is_stale(max_age_min: float = None) -> bool:
    """v12.8: True when VIX has no fresh feed update within the max age. A stale
    VIX means we are blind to regime, so the risk engine fails conservative
    (blocks new longs) instead of trusting the calm 18.0 default — closing the
    'dormant VIX sensor' gap where circuit breakers never fired without market_fn."""
    if max_age_min is None:
        max_age_min = float(os.getenv("KTRADE_MAX_VIX_AGE_MINUTES", "15"))
    if not MARKET.vix_updated_at:
        return True
    try:
        age = (datetime.now() - datetime.fromisoformat(MARKET.vix_updated_at)).total_seconds() / 60.0
        return age > max_age_min
    except Exception:
        return True


# ===========================================================================
# SECTION 4 â€” MACD + EMA STRATEGY (from claude_macd_ema_strategy Jun 10)
# ===========================================================================
def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = _ema(close, int(fast))
    slow_ema = _ema(close, int(slow))
    macd_line = fast_ema - slow_ema
    signal_line = _ema(macd_line, int(signal))
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

class MACDEMAStrategy:
    """
    MACD crossover confirmed by price above a slow EMA.
    Uses ticker-specific VectorBT params when available.
    Signal = +1 (buy), -1 (sell), 0 (hold)
    """
    name = "MACD_EMA"

    def signal(self, df: pd.DataFrame, params: Optional[dict] = None) -> int:
        params = params or {}
        fast = int(params.get("fast", 12))
        slow = int(params.get("slow", 26))
        signal_span = int(params.get("signal", 9))
        ema_span = int(params.get("ema_span", params.get("slow_span", 200)))
        min_bars = max(ema_span, slow, signal_span) + 2
        if len(df) < min_bars:
            return 0
        close = df["close"]
        ema_slow = _ema(close, ema_span)
        macd_line, signal_line, hist = _macd(close, fast, slow, signal_span)

        price_above_ema = close.iloc[-1] > ema_slow.iloc[-1]
        macd_cross_up   = (macd_line.iloc[-2] < signal_line.iloc[-2] and
                           macd_line.iloc[-1] > signal_line.iloc[-1])
        macd_cross_down = (macd_line.iloc[-2] > signal_line.iloc[-2] and
                           macd_line.iloc[-1] < signal_line.iloc[-1])

        if price_above_ema and macd_cross_up:
            return +1
        if macd_cross_down:
            return -1
        return 0

class MACDEMAConfluenceStrategy(MACDEMAStrategy):
    """
    Adds volume surge + higher-high confirmation to base MACD strategy.
    Uses VectorBT MACD/Conviction params and optional volume_mult.
    """
    name = "MACD_EMA_CONFLUENCE"

    def signal(self, df: pd.DataFrame, params: Optional[dict] = None) -> int:
        params = params or {}
        base = super().signal(df, params=params)
        if base != 1:
            return base
        if len(df) < 20:
            return 0
        volume_mult = float(params.get("volume_mult", 1.5))
        vol_surge = df["volume"].iloc[-1] > df["volume"].iloc[-20:].mean() * volume_mult
        higher_high = df["high"].iloc[-1] > df["high"].iloc[-2]
        return +1 if (vol_surge and higher_high) else 0


# ===========================================================================
# SECTION 5 â€” ORB STRATEGY (from claude_orb_strategy Jun 10)
# ===========================================================================
@dataclass
class ORBRange:
    high:       float
    low:        float
    range_size: float
    ticker:     str
    date:       str

class ORBRangeBuilder:
    """Build opening range from the latest session. opening_minutes is parameterized."""
    @staticmethod
    def _bars_for_minutes(df: pd.DataFrame, opening_minutes: int) -> int:
        if len(df.index) > 2:
            try:
                idx = pd.to_datetime(df.index)
                minutes = max(1, int(pd.Series(idx).diff().median().total_seconds() // 60))
            except Exception:
                minutes = 5
        else:
            minutes = 5
        return max(1, int(round(float(opening_minutes) / minutes)))

    def build(self, df_15m: pd.DataFrame, ticker: str, opening_minutes: int = 30) -> Optional[ORBRange]:
        if df_15m is None or len(df_15m) < 2:
            return None
        df_15m = df_15m.sort_index()
        latest_session = pd.Timestamp(df_15m.index[-1]).date()
        opening_bars = self._bars_for_minutes(df_15m, opening_minutes)
        session_bars = df_15m[df_15m.index.date == latest_session].head(opening_bars)
        if len(session_bars) < max(1, min(2, opening_bars)):
            return None
        orb_high = float(session_bars["high"].max())
        orb_low  = float(session_bars["low"].min())
        return ORBRange(
            high=orb_high, low=orb_low,
            range_size=orb_high - orb_low,
            ticker=ticker, date=str(latest_session)
        )

class ORBSignalDetector:
    """Detects breakout above/below ORB on intraday bars."""
    def detect(self, df_5m: pd.DataFrame, orb: ORBRange, volume_mult: float = 1.2) -> int:
        if orb is None or df_5m is None or len(df_5m) < 20:
            return 0
        last = df_5m.iloc[-1]
        vol_avg = df_5m["volume"].tail(20).mean()
        if last["close"] > orb.high and last["volume"] > vol_avg * float(volume_mult):
            return +1
        if last["close"] < orb.low:
            return -1
        return 0

class ORBStrategy:
    """Full ORB adapter for Scanner interface with optional approved params."""
    name = "ORB"

    def __init__(self):
        self.builder  = ORBRangeBuilder()
        self.detector = ORBSignalDetector()
        self._orb_cache: Dict[tuple, ORBRange] = {}

    def signal(self, df: pd.DataFrame, ticker: str = "", params: Optional[dict] = None) -> int:
        params = params or {}
        opening_minutes = int(params.get("opening_minutes", 30))
        volume_mult = float(params.get("volume_mult", 1.2))
        cache_key = (ticker, opening_minutes, str(pd.Timestamp(df.index[-1]).date()) if df is not None and len(df) else "")
        if cache_key not in self._orb_cache:
            orb = self.builder.build(df, ticker, opening_minutes=opening_minutes)
            if orb:
                self._orb_cache[cache_key] = orb
        orb = self._orb_cache.get(cache_key)
        return self.detector.detect(df, orb, volume_mult=volume_mult) if orb else 0


# ===========================================================================
# SECTION 6 â€” CONVICTION SCORER (from claude_heartbeat Jun 10)
# ===========================================================================
@dataclass
class ConvictionScore:
    ticker:     str
    score:      float   # 0-100
    components: Dict[str, float] = field(default_factory=dict)
    signal:     int     = 0      # +1 / -1 / 0
    strategy:   str     = ""
    price:      float   = 0.0
    atr:        float   = 0.0
    intraday_range_pct: float = 0.0   # v12.9: latest-session high-low range %


def _intraday_range_pct(df) -> float:
    """v12.9: latest-session intraday high-low range as a percent of the low.
    Used by the whipsaw gate to skip names in extreme single-session chaos
    (halt/reopen, pump-and-dump, glitch) where entry is unsafe regardless of
    conviction — the volatility sibling of the price-sanity bad-tick gate.
    Returns 0.0 when it cannot be computed."""
    try:
        if df is None or len(df) == 0 or "high" not in df or "low" not in df:
            return 0.0
        last = df.iloc[-1]
        hi = float(last["high"]); lo = float(last["low"])
        if lo <= 0:
            return 0.0
        return (hi - lo) / lo * 100.0
    except Exception:
        return 0.0


def _max_intraday_range_pct() -> float:
    """v12.9: whipsaw threshold (latest-session high-low range %) above which new
    longs are refused. Read dynamically from the env so it can be tuned at runtime;
    0 disables the gate."""
    try:
        return float(os.getenv("KTRADE_MAX_INTRADAY_RANGE_PCT", "40"))
    except (TypeError, ValueError):
        return 40.0


class ConvictionScorer:
    """
    Scores tickers across weighted components.
    Daily and intraday scoring both use approved VectorBT params when present.
    """
    WEIGHTS_DAILY = {
        "momentum": 0.33,
        "volume":   0.22,
        "trend":    0.23,
        "macd":     0.16,
        "rs":       0.06,
    }
    WEIGHTS_INTRADAY = {
        "momentum": 0.30,
        "volume":   0.20,
        "trend":    0.20,
        "macd":     0.15,
        "orb":      0.10,
        "rs":       0.05,
    }
    WEIGHTS = WEIGHTS_INTRADAY

    def __init__(self):
        self.macd_strat = MACDEMAConfluenceStrategy()
        self.orb_strat  = ORBStrategy()

    @staticmethod
    def _ret(close: pd.Series, periods: int) -> float:
        periods = max(1, int(periods))
        if len(close) <= periods or close.iloc[-periods] == 0:
            return 0.0
        return (close.iloc[-1] / close.iloc[-periods] - 1) * 100

    @staticmethod
    def _safe_int(params: dict, key: str, default: int) -> int:
        try:
            return int(params.get(key, default))
        except Exception:
            return default

    @staticmethod
    def _add_intraday_columns(df: pd.DataFrame, opening_minutes: int = 30) -> pd.DataFrame:
        x = df.copy().sort_index()
        x["date"] = pd.to_datetime(x.index).date
        x["bar_in_day"] = x.groupby("date").cumcount()
        typical = (x["high"] + x["low"] + x["close"]) / 3.0
        vol = x["volume"].replace(0, np.nan)
        x["vwap"] = (typical * vol).groupby(x["date"]).cumsum() / vol.groupby(x["date"]).cumsum()
        if len(x.index) > 2:
            try:
                minutes = max(1, int(pd.Series(pd.to_datetime(x.index)).diff().median().total_seconds() // 60))
            except Exception:
                minutes = 5
        else:
            minutes = 5
        opening_bars = max(1, int(round(float(opening_minutes) / minutes)))
        opening = x[x["bar_in_day"] < opening_bars]
        x["or_high"] = x["date"].map(opening.groupby("date")["high"].max())
        x["or_low"] = x["date"].map(opening.groupby("date")["low"].min())
        x["after_opening_range"] = x["bar_in_day"] >= opening_bars
        x["vol_ma"] = x["volume"].rolling(20).mean()
        x["ema_fast"] = x["close"].ewm(span=9, adjust=False).mean()
        x["ema_slow"] = x["close"].ewm(span=21, adjust=False).mean()
        return x

    def _orb_vwap_signal(self, df: pd.DataFrame, params: Optional[dict] = None) -> int:
        params = params or {}
        if df is None or len(df) < 30:
            return 0
        opening_minutes = int(params.get("opening_minutes", 30))
        volume_mult = float(params.get("volume_mult", 1.1))
        x = self._add_intraday_columns(df, opening_minutes)
        last = x.iloc[-1]
        prev = x.iloc[-2]
        breakout = last["close"] > last["or_high"] and prev["close"] <= prev["or_high"]
        confirmed = last["after_opening_range"] and last["close"] > last["vwap"] and last["volume"] > last["vol_ma"] * volume_mult
        return 1 if breakout and confirmed else 0

    def score(self, ticker: str, df: pd.DataFrame, interval: str = "1d", benchmark_ret5: Optional[float] = None) -> ConvictionScore:
        intraday = str(interval).lower() in INTRADAY_INTERVALS
        if df is None or len(df) < 50:
            return ConvictionScore(ticker=ticker, score=0)

        close  = df["close"]
        volume = df["volume"]
        components = {}

        macd_params = get_ticker_params(APPROVED_PARAMS, ticker, "MACD") or get_ticker_params(APPROVED_PARAMS, ticker, "CONVICTION")
        ema_params = get_ticker_params(APPROVED_PARAMS, ticker, "EMA")
        mom_params = get_ticker_params(APPROVED_PARAMS, ticker, "MOMENTUM")
        orb_params = get_ticker_params(APPROVED_INTRADAY_PARAMS, ticker, "ORB_VWAP") or get_ticker_params(APPROVED_INTRADAY_PARAMS, ticker, "ORB")

        # Momentum: use approved VectorBT lookback period when available.
        mom_period = self._safe_int(mom_params, "period", 20)
        ret5  = self._ret(close, 5)
        ret_n = self._ret(close, mom_period)
        components["momentum"] = min(100, max(0, 50 + ret5 * 3 + ret_n * 1.5))

        # Volume surge.
        vol_ratio = volume.iloc[-1] / volume.iloc[-20:].mean() if len(volume) >= 20 and volume.iloc[-20:].mean() else 1
        components["volume"] = min(100, max(0, vol_ratio * 50))

        # Trend: use approved EMA spans when available.
        fast_span = self._safe_int(ema_params, "fast_span", 50)
        slow_span = self._safe_int(ema_params, "slow_span", 200)
        ema_fast = _ema(close, fast_span).iloc[-1] if len(close) >= fast_span else close.iloc[-1]
        ema_slow = _ema(close, slow_span).iloc[-1] if len(close) >= slow_span else close.iloc[-1]
        above_fast = close.iloc[-1] > ema_fast
        above_slow = close.iloc[-1] > ema_slow
        components["trend"] = (above_fast * 50) + (above_slow * 50)

        # MACD: use approved MACD/Conviction params.
        macd_sig = self.macd_strat.signal(df, params=macd_params)
        components["macd"] = 75 if macd_sig == 1 else 25 if macd_sig == -1 else 50

        # Intraday ORB/VWAP logic from intraday-approved params.
        orb_sig = 0
        orb_vwap_sig = 0
        if intraday:
            orb_sig = self.orb_strat.signal(df, ticker, params=orb_params)
            orb_vwap_sig = self._orb_vwap_signal(df, orb_params)
            components["orb"] = 90 if orb_vwap_sig == 1 else 80 if orb_sig == 1 else 20 if orb_sig == -1 else 50

        # Actual relative strength vs SPY if benchmark is available; otherwise self-only fallback.
        if benchmark_ret5 is None:
            relative_5d = ret5
        else:
            relative_5d = ret5 - float(benchmark_ret5)
        components["rs"] = min(100, max(0, 50 + relative_5d * 2))

        weights = self.WEIGHTS_INTRADAY if intraday else self.WEIGHTS_DAILY
        total = sum(components[k] * w for k, w in weights.items())
        signal = +1 if total >= CFG.min_conviction_score else 0

        if macd_sig == 1:
            strategy = "MACD_EMA"
        elif intraday and orb_vwap_sig == 1:
            strategy = "ORB_VWAP"
        elif intraday and orb_sig == 1:
            strategy = "ORB"
        elif components["trend"] >= 100:
            strategy = "TREND"
        else:
            strategy = "MOMENTUM"

        # v12.9: whipsaw gate — a name in extreme single-session chaos is unsafe
        # to enter regardless of conviction. Record the range and, if it exceeds
        # the limit, downgrade a BUY signal to WATCH so it never reaches execution.
        intraday_range_pct = _intraday_range_pct(df)
        components["intraday_range_pct"] = round(intraday_range_pct, 1)
        _max_range = _max_intraday_range_pct()
        if _max_range and signal == +1 and intraday_range_pct >= _max_range:
            log.warning("WHIPSAW skip %s: intraday range %.0f%% >= %.0f%%",
                        ticker, intraday_range_pct, _max_range)
            signal = 0
            components["whipsaw_blocked"] = 1.0

        return ConvictionScore(
            ticker=ticker, score=round(total, 1),
            components=components, signal=signal,
            strategy=strategy, price=float(close.iloc[-1]),
            atr=self._calc_atr(df), intraday_range_pct=round(intraday_range_pct, 1)
        )

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period + 1:
            return 0.0
        high, low, close = df["high"], df["low"], df["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return round(float(tr.rolling(period).mean().iloc[-1]), 2)

    def rank_universe(self, data_map: Dict[str, pd.DataFrame], interval: str = "1d") -> List[ConvictionScore]:
        # v10.7: normalize + drop unclosed last bar before scoring (data hygiene).
        if normalize_ohlcv_frame is not None:
            clean = {}
            for tkr, raw in (data_map or {}).items():
                try:
                    df = raw
                    if drop_unclosed_last_bar is not None:
                        df = drop_unclosed_last_bar(df, interval)
                    clean[tkr] = normalize_ohlcv_frame(tkr, df, interval, min_rows=50)
                except Exception as exc:
                    log.warning("Skipping %s: invalid market frame: %s", tkr, exc)
            data_map = clean
        scores = []
        benchmark_ret5 = None
        spy = data_map.get("SPY")
        if spy is not None and len(spy) >= 6:
            benchmark_ret5 = self._ret(spy["close"], 5)
        for ticker, df in data_map.items():
            try:
                scores.append(self.score(ticker, df, interval, benchmark_ret5=benchmark_ret5))
            except Exception as e:
                log.warning(f"Score failed {ticker}: {e}")
        return sorted(scores, key=lambda x: x.score, reverse=True)


# ===========================================================================
# SECTION 7 â€” RISK ENGINE (KTrade session, Jun 15 â€” enhanced)
# ===========================================================================
@dataclass
class TradeRequest:
    ticker:      str
    side:        str        # "buy" or "sell"
    qty:         float
    price:       float
    conviction:  int  = 80
    strategy:    str  = ""
    option_type: str  = ""
    spread:      float = 0.0
    atr:         float = 0.0
    iv:          float = 0.0
    desired_risk_dollars: float = 0.0   # v10.3: CEO states risk budget; RiskEngine sizes

@dataclass
class RiskDecision:
    approved:     bool
    reason:       str
    ticker:       str
    side:         str
    original_qty: float
    approved_qty: float = 0.0
    stop_price:   float = 0.0
    target_price: float = 0.0
    dollar_risk:  float = 0.0
    warnings:     List[str] = field(default_factory=list)
    timestamp:    str = field(default_factory=lambda: datetime.now().isoformat())

class RiskEngine:
    """
    Hard-coded logic wrapper. Sits between AI signals and broker.
    ALL decisions here are deterministic math â€” no ML, no AI.
    """
    def __init__(self):
        self.equity           = CFG.account_value
        self.equity_open      = CFG.account_value
        self.daily_pnl        = 0.0
        self.kill_active      = False
        self.open_positions   = {}
        self.short_positions  = {}
        self.last_order_time  = {}   # "TICKER_side" â†’ datetime
        self.ticker_day_count = {}   # ticker â†’ int
        self.last_fill_time   = {}   # ticker â†’ datetime
        self.approved_today   = 0
        self.blocked_today    = 0
        self.price_refs       = {}   # ticker -> trusted reference (prior close / last good)
        self.intraday_ranges  = {}   # v12.9: ticker -> intraday range % for the whipsaw gate
        self.emergency        = None  # v10.7: EmergencyController (set by CEO)
        self.state_store      = None  # v10.7: RiskStateStore (set by CEO)
        self.earnings_cal     = None  # v10.9: EarningsCalendar (set by CEO)
        log.info(f"RiskEngine v10.7 | Equity: ${self.equity:,.0f} | MDD: {CFG.max_daily_drawdown_pct}%")

    # ---- v10.5: sector / correlation concentration helpers ----
    def _sector_state(self, sector: str):
        """Return (open_count, dollar_value) for a correlated sector, using
        the best available per-ticker price (live ref, else avg cost)."""
        cnt = 0
        val = 0.0
        for tkr, pos in self.open_positions.items():
            if sector_of(tkr) != sector:
                continue
            cnt += 1
            ref = self.price_refs.get(tkr) or (pos.get("avg_cost", 0) if isinstance(pos, dict) else 0)
            qty = pos.get("qty", 0) if isinstance(pos, dict) else 0
            val += float(qty) * float(ref or 0)
        return cnt, val

    def _check_sector_cap(self, trade, qty, warnings):
        """Block a buy that would over-concentrate one correlated basket.
        Returns a RiskDecision block, or None if OK."""
        if trade.side != "buy":
            return None
        sector = sector_of(trade.ticker)
        if sector == "OTHER":
            return None
        cnt, val = self._sector_state(sector)
        # don't double-count if we're adding to an existing position in-sector
        already_open = trade.ticker.upper() in self.open_positions
        if not already_open and cnt >= CFG.max_positions_per_sector:
            return self._block(trade,
                f"SECTOR CAP: {sector} already holds {cnt} positions "
                f"(max {CFG.max_positions_per_sector})", warnings)
        new_val = float(qty) * float(trade.price)
        proj_pct = (val + new_val) / self.equity * 100 if self.equity else 0
        if proj_pct > CFG.max_sector_exposure_pct:
            return self._block(trade,
                f"SECTOR EXPOSURE CAP: {sector} -> {proj_pct:.0f}% "
                f"> {CFG.max_sector_exposure_pct:.0f}% of equity", warnings)
        if proj_pct > CFG.max_sector_exposure_pct * 0.8:
            warnings.append(f"{sector} concentration {proj_pct:.0f}% nearing cap")
        return None

    def evaluate(self, trade: TradeRequest) -> RiskDecision:
        warnings = []
        now = datetime.now()

        # 1. Kill switch
        if self.kill_active:
            return self._block(trade, "KILL SWITCH ACTIVE", warnings)

        # 1b. v10.3 HARD BAD-TICK GATE -- the exact KLAC decimal-shift failure.
        #     A 10x spike reads as max momentum, so it must be blocked here,
        #     at the final approval path, not only at quote ingestion.
        if PRICE_GUARD is not None:
            ref = self.price_refs.get(trade.ticker)
            gate = PRICE_GUARD.validate_entry(trade.ticker, trade.price, reference=ref)
            # v10.6: a first BUY with NO reference cannot be bad-tick validated,
            # so refuse it when require_price_reference is on (default).
            if (CFG.require_price_reference and trade.side == "buy"
                    and gate.get("reason") == "no_reference"):
                return self._block(trade, f"NO PRICE REFERENCE for {trade.ticker}; refusing first BUY until prior close is seeded", warnings)
            if not gate["ok"]:
                return self._block(trade, f"BAD PRICE: {gate['reason']} (px={trade.price}, ref={gate['reference']})", warnings)

        # 1c. v12.9 WHIPSAW gate — refuse new longs in a name whose intraday range
        #     is extreme. Complements 1b: the bad-tick gate catches decimal-shift
        #     glitches; this catches a legitimate-but-violent range (halt/reopen,
        #     pump-dump). Only fires when a range was seeded for the ticker AND it
        #     exceeds the threshold, so unknown ranges and sells pass untouched.
        _max_range = _max_intraday_range_pct()
        if trade.side == "buy" and _max_range:
            rng = self.intraday_ranges.get(trade.ticker.upper())
            if rng is not None and rng >= _max_range:
                return self._block(trade, f"WHIPSAW — {trade.ticker} intraday range "
                                          f"{rng:.0f}% >= {_max_range:.0f}%", warnings)

        # 2. Flash crash
        if MARKET.flash_crash_active:
            return self._block(trade, f"FLASH CRASH â€” SPY down {CFG.flash_crash_drop_pct}% â€” halted", warnings)

        # 3. VIX circuit breakers
        if MARKET.vix >= CFG.vix_close_all:
            self._close_all()
            return self._block(trade, f"VIX EMERGENCY ({MARKET.vix:.1f}) â€” close all", warnings)
        if MARKET.vix >= CFG.vix_risk_off and trade.side == "buy":
            return self._block(trade, f"VIX RISK-OFF ({MARKET.vix:.1f} > {CFG.vix_risk_off})", warnings)
        if MARKET.vix >= CFG.vix_elevated:
            warnings.append(f"Elevated VIX ({MARKET.vix:.1f}) â€” size reduced 50%")
        if MARKET.vix >= CFG.vix_auto_hedge and trade.side == "buy":
            warnings.append(f"VIX={MARKET.vix:.1f} â€” consider PUT hedge on this position")

        # 4. Daily drawdown
        dd_pct = (self.equity_open - self.equity) / self.equity_open * 100
        if dd_pct >= CFG.max_daily_drawdown_pct:
            self.kill_active = True
            return self._block(trade, f"MAX DRAWDOWN HIT ({dd_pct:.1f}%)", warnings)
        if abs(self.daily_pnl) >= CFG.max_daily_loss_dollars:
            self.kill_active = True
            return self._block(trade, f"DAILY LOSS LIMIT (${abs(self.daily_pnl):,.0f})", warnings)

        # 5. Conviction gate
        if trade.conviction < CFG.min_conviction_score:
            return self._block(trade, f"LOW CONVICTION ({trade.conviction} < {CFG.min_conviction_score})", warnings)

        # 6a. FIX: Duplicate order prevention
        order_key = f"{trade.ticker}_{trade.side}"
        if order_key in self.last_order_time:
            elapsed = (now - self.last_order_time[order_key]).total_seconds()
            if elapsed < CFG.duplicate_window_sec:
                return self._block(trade, f"DUPLICATE BLOCKED â€” {trade.ticker} sent {elapsed:.0f}s ago", warnings)

        day_count = self.ticker_day_count.get(trade.ticker, 0)
        if day_count >= CFG.max_ticker_per_day:
            return self._block(trade, f"TICKER DAY LIMIT â€” {trade.ticker} traded {day_count}x today", warnings)

        # 6b. FIX: Post-fill cooldown
        if trade.ticker in self.last_fill_time:
            since = (now - self.last_fill_time[trade.ticker]).total_seconds()
            if since < CFG.ticker_cooldown_sec:
                remaining = int(CFG.ticker_cooldown_sec - since)
                return self._block(trade, f"COOLDOWN â€” {trade.ticker} filled {since:.0f}s ago, wait {remaining}s", warnings)

        # 6c. FIX: Short exposure check
        if trade.side == "sell" and trade.ticker not in self.open_positions:
            if not CFG.allow_shorts:
                return self._block(trade, "SHORTS DISABLED", warnings)
            total_short = sum(self.short_positions.values())
            new_short = trade.qty * trade.price
            if (total_short + new_short) / self.equity * 100 > CFG.max_short_exposure_pct:
                return self._block(trade, f"SHORT EXPOSURE CAP ({CFG.max_short_exposure_pct}%)", warnings)

        # 7. Max positions
        if len(self.open_positions) >= CFG.max_open_positions and trade.side == "buy":
            return self._block(trade, f"MAX POSITIONS ({CFG.max_open_positions})", warnings)

        # 8. Options spread
        if trade.option_type and trade.spread > 0:
            spread_pct = trade.spread / trade.price * 100
            if spread_pct > 0.5:
                return self._block(trade, f"SPREAD TOO WIDE ({spread_pct:.2f}%)", warnings)

        # 9. Position sizing
        qty, stop, target, risk = self._size(trade, warnings)
        if qty <= 0:
            return self._block(trade, "SIZE = 0 after risk limits", warnings)

        # 10. Max position %
        pos_pct = qty * trade.price / self.equity * 100
        if pos_pct > CFG.max_position_size_pct:
            qty = int(self.equity * CFG.max_position_size_pct / 100 / trade.price)
            warnings.append(f"Size capped at {CFG.max_position_size_pct}% of account")
        if qty <= 0:
            return self._block(trade, "SIZE = 0 after position % cap", warnings)

        # 10a. v10.9 earnings blackout — don't OPEN new risk right before a
        #      binary earnings event (the "avoid MU earnings" behavior).
        if (trade.side == "buy" and self.earnings_cal is not None
                and CFG.earnings_blackout_days > 0):
            try:
                blk, edate = self.earnings_cal.in_blackout(trade.ticker, CFG.earnings_blackout_days)
            except Exception:
                blk, edate = False, None
            if blk:
                return self._block(trade,
                    f"EARNINGS BLACKOUT — {trade.ticker} reports {edate} (within {CFG.earnings_blackout_days}d)",
                    warnings)

        # 10b. v10.5 sector / correlation concentration cap
        sector_block = self._check_sector_cap(trade, qty, warnings)
        if sector_block is not None:
            return sector_block

        # â”€â”€ APPROVED â”€â”€
        self.approved_today += 1
        self.last_order_time[order_key] = now
        self.ticker_day_count[trade.ticker] = day_count + 1
        log.info(f"âœ… APPROVED {trade.side.upper()} {qty}x {trade.ticker} | stop=${stop:.2f} target=${target:.2f} risk=${risk:.0f}")
        for w in warnings:
            log.warning(f"  âš  {w}")
        return RiskDecision(approved=True, reason="All checks passed",
            ticker=trade.ticker, side=trade.side,
            original_qty=trade.qty, approved_qty=qty,
            stop_price=round(stop, 2), target_price=round(target, 2),
            dollar_risk=round(risk, 2), warnings=warnings)

    def _size(self, trade: TradeRequest, warnings: list) -> Tuple[float, float, float, float]:
        price = trade.price
        atr   = trade.atr or price * CFG.default_stop_pct / 100
        stop_dist   = atr * CFG.atr_risk_multiplier
        stop_price  = price - stop_dist if trade.side == "buy" else price + stop_dist
        target_price= price + stop_dist * (CFG.default_target_pct / CFG.default_stop_pct) if trade.side == "buy" else price - stop_dist * 2

        # Kelly
        W = CFG.kelly_win_rate; R = CFG.kelly_avg_win
        kelly_qty = int(self.equity * max(0, (W - (1-W)/R)) * CFG.kelly_fraction / price)

        # Volatility adj
        vol_factor = 0.5 if MARKET.vix >= CFG.vix_elevated else 1.0
        kelly_adj  = int(kelly_qty * vol_factor)

        # Risk cap -- hard ceiling on dollars at risk per trade. v10.3.
        risk_per_share = abs(price - stop_price)
        risk_budget = trade.desired_risk_dollars or CFG.max_trade_dollar_risk
        risk_budget = min(risk_budget, CFG.max_trade_dollar_risk)
        cap_qty = int(risk_budget / risk_per_share) if risk_per_share > 0 else 0

        # RiskEngine is the SOLE sizer: size from the risk budget (risk-dollar
        # sizing), then clamp by Kelly. CEO no longer pre-sizes a share count.
        candidates = [c for c in (cap_qty, kelly_adj) if c > 0]
        final_qty = min(candidates) if candidates else 0
        dollar_risk = final_qty * risk_per_share
        if final_qty <= 0:
            warnings.append(f"Risk sizing to 0 (risk/share=${risk_per_share:.2f}, budget=${risk_budget:.0f})")
        else:
            warnings.append(f"Sized {final_qty} (risk-budget:{cap_qty} Kelly:{kelly_adj})")
        return final_qty, stop_price, target_price, dollar_risk

    def record_fill(self, ticker: str, side: str, qty: float, price: float):
        self.last_fill_time[ticker] = datetime.now()
        tkr = ticker.upper()
        # v10.7: partial-fill aware. A SELL reduces qty (weighted avg cost kept)
        # instead of popping the whole long; a BUY blends average cost.
        if side == "buy" and tkr in self.short_positions:
            self.short_positions.pop(tkr, None)
        elif side == "sell" and tkr not in self.open_positions:
            self.short_positions[tkr] = self.short_positions.get(tkr, 0) + qty * price
        elif apply_fill_to_position is not None:
            apply_fill_to_position(self.open_positions, tkr, side, qty, price)
        else:
            if side == "buy":
                existing = self.open_positions.get(tkr, {"qty": 0, "avg_cost": price})
                self.open_positions[tkr] = {"qty": existing["qty"] + qty, "avg_cost": price}
            elif tkr in self.open_positions:
                self.open_positions.pop(tkr, None)
        self.price_refs[tkr] = price
        log.info(f"Fill recorded: {side.upper()} {qty}x {ticker} -- cooldown started")
        self.persist_state()

    def update_equity(self, equity: float):
        self.daily_pnl = equity - self.equity_open
        self.equity    = equity
        dd = (self.equity_open - equity) / self.equity_open * 100
        if dd > CFG.max_daily_drawdown_pct * 0.8:
            log.warning(f"âš  Approaching MDD: {dd:.1f}% of {CFG.max_daily_drawdown_pct}%")

    # ---- v10.3: broker-truth sync (positions / equity / references) ----
    def sync_positions(self, broker_positions: list):
        """Replace in-memory position state with broker truth.
        broker_positions: list of dicts with at least 'ticker','shares','avgCost'
        (the shape returned by backend.ktrade_alpaca.fetch_positions())."""
        self.open_positions = {}
        self.short_positions = {}
        for p in broker_positions or []:
            tkr = (p.get("ticker") or p.get("symbol") or "").upper()
            if not tkr:
                continue
            shares = float(p.get("shares", p.get("qty", 0)) or 0)
            cost   = float(p.get("avgCost", p.get("avg_entry_price", 0)) or 0)
            if shares > 0:
                self.open_positions[tkr] = {"qty": shares, "avg_cost": cost}
            elif shares < 0:
                self.short_positions[tkr] = abs(shares) * cost
            cur = float(p.get("currentPrice", p.get("current_price", 0)) or 0)
            if cur > 0:
                self.price_refs[tkr] = cur
        log.info(f"Synced broker positions: {len(self.open_positions)} long, {len(self.short_positions)} short")

    def seed_references(self, refs: dict):
        """Seed trusted price references (e.g. prior closes) so the FIRST live
        tick of the day can be bad-tick validated. Also seeds PRICE_GUARD."""
        for tkr, px in (refs or {}).items():
            try:
                px = float(px)
            except (TypeError, ValueError):
                continue
            if px > 0:
                self.price_refs[tkr.upper()] = px
                if PRICE_GUARD is not None:
                    PRICE_GUARD.seed_reference(tkr, px)

    def seed_intraday_ranges(self, ranges: dict):
        """v12.9: seed each scanned ticker's intraday range % (high-low)/low so the
        whipsaw gate can reject names having a violently wide day (halt/reopen,
        pump-dump, or a glitch that slipped past the bad-tick gate)."""
        for tkr, pct in (ranges or {}).items():
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                continue
            if pct >= 0:
                self.intraday_ranges[tkr.upper()] = pct
    def activate_kill_switch(self, reason: str = "Manual", flatten: bool = False):
        self.kill_active = True
        log.critical(f"KILL SWITCH: {reason}")
        if self.emergency is not None:
            try:
                self.emergency.trigger(reason, flatten=flatten)
            except Exception as exc:
                log.error(f"emergency.trigger failed: {exc}")
        self.persist_state()

    def reset_day(self):
        self.kill_active      = False
        self.daily_pnl        = 0.0
        self.equity_open      = self.equity
        self.approved_today   = 0
        self.blocked_today    = 0
        self.ticker_day_count = {}
        self.last_order_time  = {}
        log.info("âœ… Risk engine reset for new day")

    def _close_all(self):
        log.critical("CLOSE ALL POSITIONS triggered")
        # v10.7: actually cancel open orders (and flatten) via the emergency
        # controller's broker, instead of only logging.
        if self.emergency is not None:
            try:
                self.emergency.trigger("RiskEngine close-all", flatten=True)
            except Exception as exc:
                log.error(f"_close_all emergency failed: {exc}")

    # ---- v10.7: state persistence ----
    def serialize_state(self) -> dict:
        return {
            "kill_active": self.kill_active,
            "equity_open": self.equity_open,
            "approved_today": self.approved_today,
            "blocked_today": self.blocked_today,
            "ticker_day_count": self.ticker_day_count,
            "last_order_time": {k: v.isoformat() for k, v in self.last_order_time.items()},
            "last_fill_time": {k: v.isoformat() for k, v in self.last_fill_time.items()},
            "price_refs": self.price_refs,
        }

    def restore_state(self, data: dict) -> None:
        if not data:
            return
        try:
            self.kill_active = bool(data.get("kill_active", self.kill_active))
            self.equity_open = float(data.get("equity_open", self.equity_open))
            self.approved_today = int(data.get("approved_today", 0))
            self.blocked_today = int(data.get("blocked_today", 0))
            self.ticker_day_count = dict(data.get("ticker_day_count", {}))
            self.price_refs.update(data.get("price_refs", {}) or {})
            for k, v in (data.get("last_order_time", {}) or {}).items():
                try: self.last_order_time[k] = datetime.fromisoformat(v)
                except Exception: pass
            for k, v in (data.get("last_fill_time", {}) or {}).items():
                try: self.last_fill_time[k] = datetime.fromisoformat(v)
                except Exception: pass
            log.info("Restored persisted risk state")
        except Exception as exc:
            log.warning(f"restore_state failed: {exc}")

    def persist_state(self) -> None:
        if self.state_store is not None:
            try:
                self.state_store.save(self.serialize_state())
            except Exception as exc:
                log.debug(f"persist_state failed: {exc}")

    def _block(self, trade: TradeRequest, reason: str, warnings: list) -> RiskDecision:
        self.blocked_today += 1
        log.warning(f"ðŸš« BLOCKED {trade.side.upper()} {trade.ticker}: {reason}")
        return RiskDecision(approved=False, reason=reason,
            ticker=trade.ticker, side=trade.side,
            original_qty=trade.qty, approved_qty=0, warnings=warnings)

    def status(self) -> dict:
        dd = (self.equity_open - self.equity) / self.equity_open * 100
        return {"kill_active": self.kill_active, "vix": MARKET.vix,
                "flash_crash": MARKET.flash_crash_active,
                "daily_pnl": round(self.daily_pnl, 2),
                "drawdown_pct": round(dd, 2), "equity": round(self.equity, 2),
                "approved_today": self.approved_today, "blocked_today": self.blocked_today}


# ===========================================================================
# SECTION 8 â€” CRASH DETECTION AGENT (from Jun 5 session)
# ===========================================================================
class CrashDetectionAgent:
    """
    Monitors macro signals to detect crash conditions BEFORE they happen.
    Outputs a crash_risk_score (0-100) and recommended puts.
    """
    def __init__(self):
        self.history: List[Dict] = []

    def evaluate(self, vix: float, put_call_ratio: float,
                 spy_5d_return: float, advance_decline: float,
                 high_yield_spread: float = 3.5) -> Dict[str, Any]:
        score = 0.0
        signals = []

        # VIX spike (weight: 30)
        if vix > 40:   score += 30; signals.append(f"VIX EXTREME ({vix:.1f})")
        elif vix > 30: score += 20; signals.append(f"VIX HIGH ({vix:.1f})")
        elif vix > 25: score += 10; signals.append(f"VIX ELEVATED ({vix:.1f})")

        # Put/Call ratio (weight: 25)
        if put_call_ratio > 1.5:   score += 25; signals.append(f"P/C EXTREME ({put_call_ratio:.2f})")
        elif put_call_ratio > 1.2: score += 15; signals.append(f"P/C HIGH ({put_call_ratio:.2f})")
        elif put_call_ratio > 1.0: score += 8;  signals.append(f"P/C ELEVATED ({put_call_ratio:.2f})")

        # SPY 5-day return (weight: 25)
        if spy_5d_return < -5:   score += 25; signals.append(f"SPY 5D: {spy_5d_return:.1f}%")
        elif spy_5d_return < -3: score += 15; signals.append(f"SPY 5D: {spy_5d_return:.1f}%")
        elif spy_5d_return < -1: score += 5;  signals.append(f"SPY 5D: {spy_5d_return:.1f}%")

        # Advance/Decline (weight: 10)
        if advance_decline < 0.5:   score += 10; signals.append(f"A/D WEAK ({advance_decline:.2f})")
        elif advance_decline < 0.8: score += 5;  signals.append(f"A/D LOW ({advance_decline:.2f})")

        # High yield spread (weight: 10)
        if high_yield_spread > 6:   score += 10; signals.append(f"HY SPREAD WIDE ({high_yield_spread:.1f}%)")
        elif high_yield_spread > 4: score += 5;  signals.append(f"HY SPREAD ELEVATED")

        # Recommend puts
        puts = []
        if score >= 50:
            puts = [
                {"ticker": "SPY", "type": "PUT", "dte": 30, "reason": "Index hedge"},
                {"ticker": "QQQ", "type": "PUT", "dte": 45, "reason": "Tech crash hedge"},
            ]
        elif score >= 30:
            puts = [{"ticker": "SPY", "type": "PUT", "dte": 30, "reason": "Precautionary hedge"}]

        MARKET.crash_risk_score = score
        result = {"score": score, "signals": signals, "puts": puts,
                  "risk_level": "HIGH" if score >= 60 else "MEDIUM" if score >= 30 else "LOW",
                  "timestamp": datetime.now().isoformat()}
        self.history.append(result)
        return result


# ===========================================================================
# SECTION 9 â€” DAILY LOSS GUARD (from claude_heartbeat Jun 10)
# ===========================================================================
class DailyLossGuard:
    """Hard kill switch based on daily P&L. Separate from RiskEngine."""
    def __init__(self, limit_pct: float = None):
        self.limit_pct   = limit_pct or CFG.daily_loss_limit_pct
        self.start_equity = CFG.account_value
        self.triggered   = False

    def check(self, current_equity: float) -> bool:
        if self.triggered:
            return True
        loss_pct = (self.start_equity - current_equity) / self.start_equity
        if loss_pct >= self.limit_pct:
            log.critical(f"ðŸ›‘ DAILY LOSS GUARD: lost {loss_pct*100:.1f}% â€” halting all trading")
            self.triggered = True
        return self.triggered

    def reset(self, new_equity: float):
        self.start_equity = new_equity
        self.triggered    = False


# ===========================================================================
# SECTION 10 â€” POSITION MONITOR (from claude_heartbeat Jun 10)
# ===========================================================================
@dataclass
class TrackedPosition:
    ticker:       str
    entry_price:  float
    qty:          float
    stop_price:   float
    target_price: float
    entry_time:   datetime = field(default_factory=datetime.now)
    peak_price:   float    = 0.0
    trailing_stop: float   = 0.0

class PositionMonitor:
    """Auto-exits positions at stop or target (1.5R default)."""
    def __init__(self, broker_fn=None):
        self.positions: Dict[str, TrackedPosition] = {}
        self.broker_fn = broker_fn  # callable(ticker, side, qty) â†’ order

    def add(self, pos: TrackedPosition):
        self.positions[pos.ticker] = pos
        pos.peak_price    = pos.entry_price
        pos.trailing_stop = pos.entry_price - (pos.entry_price - pos.stop_price) * 1.0
        log.info(f"Tracking {pos.ticker}: stop=${pos.stop_price:.2f} target=${pos.target_price:.2f}")

    def check(self, prices: Dict[str, float]) -> List[str]:
        exits = []
        for ticker, pos in list(self.positions.items()):
            price = prices.get(ticker)
            if not price:
                continue
            # Update trailing stop
            if price > pos.peak_price:
                pos.peak_price = price
                trail = (pos.entry_price - pos.stop_price)
                pos.trailing_stop = max(pos.trailing_stop, price - trail)

            exit_reason = None
            if price <= pos.trailing_stop:
                exit_reason = f"STOP HIT (price=${price:.2f} stop=${pos.trailing_stop:.2f})"
            elif price >= pos.target_price:
                exit_reason = f"TARGET HIT (price=${price:.2f} target=${pos.target_price:.2f})"

            if exit_reason:
                log.info(f"EXIT {ticker}: {exit_reason}")
                if self.broker_fn:
                    self.broker_fn(ticker, "sell", pos.qty)
                exits.append(ticker)
                del self.positions[ticker]
        return exits


# ===========================================================================
# SECTION 11 â€” HEARTBEAT ENGINE (from claude_heartbeat Jun 10)
# ===========================================================================
class MarketPhase(Enum):
    PRE_MARKET   = "PRE"
    OPEN         = "OPEN"       # 9:30â€“10:00 (ORB window)
    REGULAR      = "REGULAR"    # 10:00â€“15:30
    POWER_HOUR   = "POWER_HOUR" # 15:30â€“16:00
    AFTER         = "AFTER"

class HeartbeatEngine:
    """Market-phase-aware scan scheduler."""
    SCAN_INTERVALS = {
        MarketPhase.PRE_MARKET:  300,  # 5 min
        MarketPhase.OPEN:        60,   # 1 min (ORB critical window)
        MarketPhase.REGULAR:     300,  # 5 min
        MarketPhase.POWER_HOUR:  120,  # 2 min
        MarketPhase.AFTER:       600,  # 10 min
    }

    # Minimal NYSE holiday set (extend yearly). v10.3.
    _HOLIDAYS_2026 = {
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    }

    def _now_et(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            return datetime.now()   # last-resort fallback

    def is_trading_day(self, now=None) -> bool:
        now = now or self._now_et()
        if now.weekday() >= 5:            # Sat/Sun
            return False
        return now.strftime("%Y-%m-%d") not in self._HOLIDAYS_2026

    def get_phase(self) -> MarketPhase:
        now = self._now_et()
        if not self.is_trading_day(now):
            return MarketPhase.AFTER
        h, m = now.hour, now.minute
        t = h * 60 + m
        if t < 9*60+30:                    return MarketPhase.PRE_MARKET
        if t < 10*60:                      return MarketPhase.OPEN
        if t < 15*60+30:                   return MarketPhase.REGULAR
        if t < 16*60:                      return MarketPhase.POWER_HOUR
        return MarketPhase.AFTER

    def scan_interval(self) -> int:
        return self.SCAN_INTERVALS[self.get_phase()]

    def is_market_open(self) -> bool:
        if not self.is_trading_day():
            return False
        phase = self.get_phase()
        return phase in (MarketPhase.OPEN, MarketPhase.REGULAR, MarketPhase.POWER_HOUR)


# ===========================================================================
# SECTION 12 â€” CEO ORCHESTRATOR (from claude_ceo_orchestrator_v2 Jun 15)
# ===========================================================================
@dataclass
class AgentMessage:
    sender:    str
    recipient: str
    type:      str      # "SIGNAL", "RISK_CHECK", "EXECUTE", "REPORT", "QUERY"
    payload:   Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

class ResearchAgent:
    """Scores tickers and surfaces top conviction plays."""
    name = "RESEARCH"
    def __init__(self): self.scorer = ConvictionScorer()

    def run(self, data_map: Dict[str, pd.DataFrame], interval: str = "1d") -> List[ConvictionScore]:
        scores = self.scorer.rank_universe(data_map, interval)
        top = [s for s in scores if s.score >= CFG.min_conviction_score]
        log.info(f"[RESEARCH] Top picks: {[(s.ticker, s.score) for s in top[:5]]}")
        return top

class StrategyAgent:
    """Runs executable strategy confirmations using approved daily/intraday params."""
    name = "STRATEGY"
    def __init__(self):
        self.macd = MACDEMAConfluenceStrategy()
        self.orb  = ORBStrategy()

    def run(self, ticker: str, df: pd.DataFrame, intraday: bool = False) -> Dict[str, int]:
        macd_params = get_ticker_params(APPROVED_PARAMS, ticker, "MACD") or get_ticker_params(APPROVED_PARAMS, ticker, "CONVICTION")
        mom_params = get_ticker_params(APPROVED_PARAMS, ticker, "MOMENTUM")
        ema_params = get_ticker_params(APPROVED_PARAMS, ticker, "EMA")
        sigs = {
            "macd":     self.macd.signal(df, params=macd_params),
            "momentum": self._momentum_signal(df, mom_params),
            "trend":    self._trend_signal(df, ema_params),
        }
        if intraday:
            orb_params = get_ticker_params(APPROVED_INTRADAY_PARAMS, ticker, "ORB_VWAP") or get_ticker_params(APPROVED_INTRADAY_PARAMS, ticker, "ORB")
            sigs["orb"] = self.orb.signal(df, ticker, params=orb_params)
            sigs["orb_vwap"] = self._orb_vwap_signal(df, orb_params)
            sigs["vwap_reclaim"] = self._vwap_reclaim_signal(df, get_ticker_params(APPROVED_INTRADAY_PARAMS, ticker, "VWAP_RECLAIM"))
            sigs["trend_continuation"] = self._trend_continuation_signal(df, get_ticker_params(APPROVED_INTRADAY_PARAMS, ticker, "TREND_CONTINUATION"))
        return sigs

    @staticmethod
    def _momentum_signal(df: pd.DataFrame, params: Optional[dict] = None) -> int:
        """Executable momentum rule: breakout/rising close using approved lookback."""
        params = params or {}
        c = df["close"]
        period = int(params.get("period", 20))
        if len(c) <= max(5, period):
            return 0
        ret5 = c.iloc[-1] / c.iloc[-5] - 1
        retp = c.iloc[-1] / c.iloc[-period] - 1
        if ret5 > 0 and retp > 0 and c.iloc[-1] > c.iloc[-2]:
            return 1
        if ret5 < 0 and retp < 0 and c.iloc[-1] < c.iloc[-2]:
            return -1
        return 0

    @staticmethod
    def _trend_signal(df: pd.DataFrame, params: Optional[dict] = None) -> int:
        """Executable trend rule using approved EMA spans."""
        params = params or {}
        c = df["close"]
        fast_span = int(params.get("fast_span", 50))
        slow_span = int(params.get("slow_span", 200))
        need = max(fast_span, slow_span, 5)
        if len(c) < need:
            return 0
        fast = _ema(c, fast_span)
        slow = _ema(c, slow_span)
        if c.iloc[-1] > slow.iloc[-1] and fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-1] > fast.iloc[-5]:
            return 1
        if c.iloc[-1] < fast.iloc[-1] and fast.iloc[-1] < slow.iloc[-1]:
            return -1
        return 0

    @staticmethod
    def _intraday_columns(df: pd.DataFrame, opening_minutes: int = 30) -> pd.DataFrame:
        return ConvictionScorer._add_intraday_columns(df, opening_minutes)

    def _orb_vwap_signal(self, df: pd.DataFrame, params: Optional[dict] = None) -> int:
        return ConvictionScorer()._orb_vwap_signal(df, params)

    def _vwap_reclaim_signal(self, df: pd.DataFrame, params: Optional[dict] = None) -> int:
        params = params or {}
        if df is None or len(df) < 30:
            return 0
        volume_mult = float(params.get("volume_mult", 1.0))
        x = self._intraday_columns(df, 30)
        last = x.iloc[-1]
        prev = x.iloc[-2]
        reclaim = last["close"] > last["vwap"] and prev["close"] <= prev["vwap"]
        trend = last["ema_fast"] > last["ema_slow"]
        volume_ok = last["volume"] > last["vol_ma"] * volume_mult
        return 1 if reclaim and trend and last["after_opening_range"] and volume_ok else 0

    def _trend_continuation_signal(self, df: pd.DataFrame, params: Optional[dict] = None) -> int:
        params = params or {}
        if df is None or len(df) < 30:
            return 0
        start_minutes = int(params.get("start_minutes", 45))
        volume_mult = float(params.get("volume_mult", 0.8))
        x = self._intraday_columns(df, start_minutes)
        last = x.iloc[-1]
        return 1 if (last["close"] > last["vwap"] and last["ema_fast"] > last["ema_slow"] and last["after_opening_range"] and last["volume"] >= last["vol_ma"] * volume_mult) else 0

class RiskAgent:
    """Wraps RiskEngine â€” approves or blocks each trade."""
    name = "RISK"
    def __init__(self): self.engine = RiskEngine()

    def evaluate(self, trade: TradeRequest) -> RiskDecision:
        return self.engine.evaluate(trade)

    def record_fill(self, ticker, side, qty, price):
        self.engine.record_fill(ticker, side, qty, price)

class ExecutionAgent:
    """Places real Alpaca bracket orders after risk approval.

    v10.3: takes an optional broker adapter. The adapter must expose
        submit_bracket(ticker, qty, side, stop, target, client_order_id) -> dict|None
        await_fill(order) -> dict|None   # {filled_qty, filled_avg_price, status}
    backend.ktrade_alpaca provides compatible primitives. When no adapter
    is wired, execute() returns a SIMULATED result (filled=False) so the CEO
    never records a phantom fill.
    """
    name = "EXECUTION"
    def __init__(self, broker=None):
        self.monitor = PositionMonitor()
        self.broker = broker

    def execute(self, decision: RiskDecision, score: ConvictionScore) -> dict:
        """Returns {'filled': bool, 'qty': float, 'price': float, 'simulated': bool, 'reason': str}."""
        if not decision.approved:
            return {"filled": False, "qty": 0, "price": 0.0, "simulated": False, "reason": "not approved"}

        # No broker wired -> SIMULATE, but do NOT claim a fill. v10.3.
        if self.broker is None:
            log.info(f"[EXECUTION:SIM] Would place {decision.side.upper()} "
                     f"{decision.approved_qty}x {decision.ticker} "
                     f"stop=${decision.stop_price:.2f} target=${decision.target_price:.2f}")
            return {"filled": False, "qty": decision.approved_qty,
                    "price": score.price, "simulated": True, "reason": "no broker adapter"}

        # Submit a real bracket order.
        try:
            coid = f"ktrade-{decision.ticker}-{decision.side}-{uuid.uuid4().hex[:12]}"
            order = self.broker.submit_bracket(
                decision.ticker, decision.approved_qty, decision.side,
                decision.stop_price, decision.target_price, client_order_id=coid)
        except Exception as e:
            log.error(f"[EXECUTION] submit failed for {decision.ticker}: {e}")
            return {"filled": False, "qty": 0, "price": 0.0, "simulated": False, "reason": f"submit error: {e}"}

        if not order:
            return {"filled": False, "qty": 0, "price": 0.0, "simulated": False, "reason": "submit returned None"}

        # Wait for broker confirmation BEFORE we treat the position as real.
        fill = None
        try:
            fill = self.broker.await_fill(order)
        except Exception as e:
            log.error(f"[EXECUTION] await_fill failed for {decision.ticker}: {e}")

        if not fill or float(fill.get("filled_qty", 0)) <= 0:
            log.warning(f"[EXECUTION] {decision.ticker} not filled (status={fill.get('status') if fill else 'unknown'})")
            return {"filled": False, "qty": 0, "price": 0.0, "simulated": False,
                    "reason": f"unfilled ({fill.get('status') if fill else 'no fill'})"}

        fqty = float(fill["filled_qty"])
        fpx  = float(fill.get("filled_avg_price") or score.price)
        pos = TrackedPosition(
            ticker=decision.ticker, entry_price=fpx, qty=fqty,
            stop_price=decision.stop_price, target_price=decision.target_price)
        self.monitor.add(pos)
        log.info(f"[EXECUTION] FILLED {decision.side.upper()} {fqty}x {decision.ticker} @ ${fpx:.2f}")
        return {"filled": True, "qty": fqty, "price": fpx, "simulated": False, "reason": "filled"}

class CostOptimizerAgent:
    """Tracks API call budget and dynamically adjusts scan frequency."""
    name = "COST"
    def __init__(self):
        self._calls_today = 0
        self._cost_today  = 0.0
        self._start_date  = date.today()

    def _roll_day(self):
        if date.today() != self._start_date:
            self._calls_today = 0; self._cost_today = 0.0
            self._start_date  = date.today()

    def record_call(self, cost: float = 0.001):
        self._roll_day()
        self._calls_today += 1
        self._cost_today  += cost

    def record_calls(self, count: int = 1, cost: float = 0.0):
        """v10.3: count N underlying API calls (e.g. one per ticker), not 1."""
        self._roll_day()
        self._calls_today += max(1, int(count))
        self._cost_today  += cost

    def budget_ok(self) -> bool:
        return (self._calls_today < CFG.max_daily_api_calls and
                self._cost_today  < CFG.cost_budget_usd)

    def status(self) -> str:
        return f"Calls: {self._calls_today}/{CFG.max_daily_api_calls} | Cost: ${self._cost_today:.3f}/${CFG.cost_budget_usd}"

# ===========================================================================
# SECTION 11c — COPILOT ADVISORY LAYER (v13.0, Milestone 2)
#   Optional LLM opinion layer over the deterministic rule decisions.
#   Modes: off / shadow / active.
#     off    : never called (zero cost).
#     shadow : produces a SKIP/BUY opinion + reasoning, LOGS it to the audit
#              ledger, but NEVER changes what trades (rules decide).
#     active : a SKIP/HOLD opinion can VETO a rule-approved BUY. It can never
#              CREATE a buy the rules did not approve.
#   On any error / missing API key it ABSTAINS and FAILS OPEN (defers to the
#   rules), so LLM downtime can never block trading. The whole point is the
#   ledger: a falsifiable record of "was the LLM right?" once outcomes are known.
# ===========================================================================
_COPILOT_ROOT = Path(__file__).resolve().parent.parent


class CopilotVerdict:
    """One copilot opinion on a single rule-approved BUY candidate."""
    VALID = ("BUY", "SKIP", "HOLD", "ABSTAIN")

    def __init__(self, verdict: str, reason: str = "", raw: str = ""):
        v = str(verdict or "ABSTAIN").upper().strip()
        self.verdict = v if v in self.VALID else "ABSTAIN"
        self.reason = (reason or "").strip()
        self.raw = raw

    @property
    def abstained(self) -> bool:
        return self.verdict == "ABSTAIN"

    @property
    def disagrees_with_buy(self) -> bool:
        # A real opinion that is not "open the long". ABSTAIN is a non-opinion.
        return self.verdict in ("SKIP", "HOLD")

    @property
    def vetoes(self) -> bool:
        # In active mode this blocks the rule BUY. ABSTAIN never vetoes (fail-open).
        return self.verdict in ("SKIP", "HOLD")


class CopilotLedger:
    """Append-only decision-context audit ledger (one JSON line per weighed-in
    decision). This is the highest-value operational artifact: it lets you score
    the copilot's calls against realized outcomes later."""

    def __init__(self, path=None):
        self.path = Path(path or os.getenv(
            "KTRADE_COPILOT_LEDGER", str(_COPILOT_ROOT / "logs" / "ktrade_copilot_ledger.jsonl")))

    def record(self, score, decision, verdict: "CopilotVerdict", mode: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            price = getattr(score, "price", None)
            qty = getattr(decision, "approved_qty", None)
            notional = None
            try:
                if price is not None and qty is not None:
                    notional = round(float(price) * float(qty), 2)
            except (TypeError, ValueError):
                notional = None
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),   # v13.1: UTC for clean cross-server scoring
                "ticker": getattr(score, "ticker", None),
                "rule_action": "BUY" if getattr(decision, "approved", False) else "BLOCK",
                "rule_reason": getattr(decision, "reason", None),
                "conviction": getattr(score, "score", None),
                "price": price,
                "qty": qty,                                     # v13.1: for notional-weighted scoring
                "notional": notional,
                "dollar_risk": getattr(decision, "dollar_risk", None),
                "strategy": getattr(score, "strategy", None),
                "copilot_verdict": verdict.verdict,
                "copilot_reason": verdict.reason,
                "mode": mode,
                "disagreement": bool(getattr(decision, "approved", False) and verdict.disagrees_with_buy),
                "vetoed": bool(mode == "active" and getattr(decision, "approved", False) and verdict.vetoes),
            }
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as exc:
            log.warning("copilot ledger write failed: %s", exc)

    def summary(self) -> dict:
        """Tally agreement / disagreement / vetoes across the ledger so far."""
        out = {"decisions": 0, "agreements": 0, "disagreements": 0, "vetoes": 0,
               "abstains": 0, "by_verdict": {}}
        try:
            if not self.path.exists():
                return out
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                out["decisions"] += 1
                v = row.get("copilot_verdict", "ABSTAIN")
                out["by_verdict"][v] = out["by_verdict"].get(v, 0) + 1
                if v == "ABSTAIN":
                    out["abstains"] += 1
                elif row.get("disagreement"):
                    out["disagreements"] += 1
                else:
                    out["agreements"] += 1
                if row.get("vetoed"):
                    out["vetoes"] += 1
        except Exception as exc:
            log.warning("copilot ledger summary failed: %s", exc)
        return out


class CopilotAdvisor:
    """LLM advisory layer. See SECTION 11c header for the mode semantics."""

    def __init__(self, ledger: "CopilotLedger" = None, ask_fn=None):
        self.mode = os.getenv("KTRADE_COPILOT_MODE", "off").strip().lower()
        if self.mode not in ("off", "shadow", "active"):
            self.mode = "off"
        self.model = os.getenv("KTRADE_COPILOT_MODEL", os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"))
        self.timeout = float(os.getenv("KTRADE_COPILOT_TIMEOUT_S", "20"))
        self.ledger = ledger or CopilotLedger()
        self._ask_fn = ask_fn   # injectable transport for tests / alt providers

    # -- prompt ------------------------------------------------------------
    def _prompt(self, score, decision, market=None, held=None) -> str:
        comps = getattr(score, "components", {}) or {}
        comp_str = ", ".join(f"{k}={v}" for k, v in comps.items()
                             if k not in ("whipsaw_blocked",))
        vix = getattr(market, "vix", None)
        regime = ""
        if vix is not None:
            regime = f"VIX={vix:.1f}"
            if getattr(market, "flash_crash_active", False):
                regime += " FLASH_CRASH"
        held_str = ", ".join(sorted(held)) if held else "none"
        return (
            "You are a risk-first trading copilot for KTrade, a PAPER-trading agent. "
            "The deterministic rule engine has ALREADY approved opening a new long in "
            f"{getattr(score, 'ticker', '?')}. Your job is a second opinion focused on "
            "reasons NOT to enter: overextension, concentration with existing holdings, "
            "poor regime fit, or known negative news in your knowledge. You do NOT have a "
            "live news feed, so do not invent catalysts; reason from the data and what you "
            "actually know.\n\n"
            f"Candidate: {getattr(score, 'ticker', '?')} @ ${getattr(score, 'price', 0):.2f}, "
            f"conviction={getattr(score, 'score', 0)}, strategy={getattr(score, 'strategy', '?')}\n"
            f"Technical components: {comp_str or 'n/a'}\n"
            f"Market regime: {regime or 'unknown'}\n"
            f"Currently held: {held_str}\n\n"
            "Reply with ONLY a compact JSON object and nothing else:\n"
            '{"verdict": "BUY" | "SKIP", "reason": "<=20 words"}\n'
            'Use BUY to agree with opening the long, SKIP to advise against it.'
        )

    # -- transport ---------------------------------------------------------
    def _call_llm(self, prompt: str) -> str:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": api_key,
                     "anthropic-version": "2023-06-01"},
            json={"model": self.model, "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("content", [{}])[0].get("text", "")

    # -- parse -------------------------------------------------------------
    @staticmethod
    def _parse(raw: str) -> "CopilotVerdict":
        text = (raw or "").strip()
        verdict, reason = None, ""
        # Prefer a JSON object if present.
        try:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end != -1 and end > start:
                obj = json.loads(text[start:end + 1])
                verdict = str(obj.get("verdict", "")).upper().strip()
                reason = str(obj.get("reason", "")).strip()
        except Exception:
            verdict = None
        if verdict not in ("BUY", "SKIP", "HOLD"):
            up = text.upper()
            if "SKIP" in up:
                verdict = "SKIP"
            elif "HOLD" in up:
                verdict = "HOLD"
            elif "BUY" in up:
                verdict = "BUY"
            else:
                verdict = "ABSTAIN"
            if not reason:
                reason = text[:160]
        return CopilotVerdict(verdict, reason, raw=text)

    # -- public ------------------------------------------------------------
    def consult(self, score, decision, market=None, held=None) -> "CopilotVerdict":
        if self.mode == "off":
            return CopilotVerdict("ABSTAIN", "copilot off")
        try:
            prompt = self._prompt(score, decision, market=market, held=held)
            raw = self._ask_fn(prompt) if self._ask_fn is not None else self._call_llm(prompt)
            return self._parse(raw)
        except Exception as exc:
            log.warning("copilot consult failed (%s) — ABSTAIN / fail-open", exc)
            return CopilotVerdict("ABSTAIN", f"error: {exc}")

    def should_block(self, verdict: "CopilotVerdict") -> bool:
        """True ONLY when an active-mode copilot vetoes a rule-approved BUY.
        shadow / off and any ABSTAIN never block (fail-open to the rules)."""
        return self.mode == "active" and verdict.vetoes


class KTradeCEO:
    """
    CEO Agent â€” orchestrates all sub-agents.
    You only talk to this. It delegates to specialists.
    """
    def __init__(self, broker=None, market_fn=None):
        """broker: optional adapter (see ExecutionAgent) used for execution
        AND for position/equity truth. market_fn: optional callable returning
        a dict {vix, spy_price, flash_crash} to refresh live market state."""
        self.broker    = broker
        self.market_fn = market_fn
        self.research  = ResearchAgent()
        self.strategy  = StrategyAgent()
        self.risk      = RiskAgent()
        self.execution = ExecutionAgent(broker=broker)
        self.cost      = CostOptimizerAgent()
        self.heartbeat = HeartbeatEngine()
        self.loss_guard= DailyLossGuard()
        self.crash_det = CrashDetectionAgent()
        self.copilot   = CopilotAdvisor()   # v13.0: LLM advisory layer (off/shadow/active)
        self._cycle    = 0
        self._running  = False
        self._broker_fail_streak = 0   # v12.8: consecutive broker-truth failures
        self._equity_initialized = False   # v10.6: set baseline from broker on first sync
        from datetime import date as _date
        self._risk_day = _date.today()     # v10.7: trading-day rollover tracking
        # v10.7: emergency controller (persistent kill + cancel/flatten) and
        # risk-state persistence, wired into the RiskEngine.
        if EmergencyController is not None:
            self.emergency = EmergencyController(broker=broker)
            self.risk.engine.emergency = self.emergency
            if self.emergency.active():
                self.risk.engine.kill_active = True
                log.critical("Startup with ACTIVE kill switch: %s", self.emergency.reason)
        else:
            self.emergency = None
        if RiskStateStore is not None:
            self.risk.engine.state_store = RiskStateStore()
            self.risk.engine.restore_state(self.risk.engine.state_store.load())
        # v10.9: earnings-event awareness
        if EarningsCalendar is not None:
            self.earnings_cal = EarningsCalendar()
            self.risk.engine.earnings_cal = self.earnings_cal
        else:
            self.earnings_cal = None

    def _earnings_blackout_exits(self) -> list:
        """v10.9: exit open holdings that are within earnings_exit_days of earnings.
        Reports each as {ticker, earnings_date, sold, reason}. Only actually sells
        when a broker sell hook is wired; otherwise it flags the intent."""
        out = []
        if not (CFG.earnings_exit_ahead and self.earnings_cal is not None):
            return out
        for tkr in list(self.risk.engine.open_positions.keys()):
            try:
                blk, edate = self.earnings_cal.in_blackout(tkr, CFG.earnings_exit_days)
            except Exception:
                blk, edate = False, None
            if not blk:
                continue
            pos = self.risk.engine.open_positions.get(tkr, {})
            qty = float(pos.get("qty", 0) or 0)
            reason = f"EARNINGS-AHEAD EXIT — {tkr} reports {edate} (within {CFG.earnings_exit_days}d)"
            sold = False
            sell_fn = getattr(self.execution.monitor, "broker_fn", None)
            if qty > 0 and sell_fn:
                try:
                    sell_fn(tkr, "sell", qty)
                    self.risk.engine.record_fill(tkr, "sell", qty, pos.get("avg_cost", 0) or 0)
                    self.execution.monitor.positions.pop(tkr, None)
                    sold = True
                except Exception as exc:
                    log.error("earnings exit sell failed for %s: %s", tkr, exc)
            log.warning("%s (sold=%s)", reason, sold)
            out.append({"ticker": tkr, "earnings_date": str(edate), "sold": sold, "reason": reason})
        return out

    def _roll_trading_day_if_needed(self, equity: float) -> None:
        """v10.7: reset daily counters / loss baseline on a new trading day so
        cooldowns and daily limits don't carry overnight."""
        from datetime import date as _date
        today = _date.today()
        if today != self._risk_day:
            self.risk.engine.reset_day()
            self.risk.engine.equity_open = equity
            self.loss_guard.reset(equity)
            self._risk_day = today
            log.info(f"New trading day {today}: risk counters reset (equity_open=${equity:,.2f})")

    def run_cycle(self, data_map: Dict[str, pd.DataFrame],
                  prices: Dict[str, float]) -> Dict[str, Any]:
        self._cycle += 1
        log.info(f"\n{'='*54}\nCYCLE {self._cycle} | {datetime.now().strftime('%H:%M:%S')} | Phase: {self.heartbeat.get_phase().value}\n{'='*54}")

        results = {"cycle": self._cycle, "trades": [], "blocked": [], "exits": []}

        # 0. Refresh broker truth + live market state BEFORE any decision. v10.3.
        equity = CFG.account_value
        if self.broker is not None:
            try:
                acct = self.broker.get_account()
                if acct and acct.get("equity"):
                    equity = float(acct["equity"])
                    # v10.6: on first real sync, set the equity BASELINE to broker
                    # truth so a real account != ACCOUNT_VALUE cannot trip a false
                    # daily-loss halt.
                    if not self._equity_initialized:
                        self.risk.engine.equity_open = equity
                        self.risk.engine.equity = equity
                        self.loss_guard.reset(equity)
                        self._equity_initialized = True
                        log.info(f"Equity baseline set from broker: ${equity:,.2f}")
                    self.risk.engine.update_equity(equity)
                self.risk.engine.sync_positions(self.broker.get_positions())
            except Exception as e:
                log.error(f"Broker sync failed -- halting cycle for safety: {e}")
                return results
        self._refresh_market_state(data_map)
        self._log_regime_shadow(data_map)     # v13.3: shadow regime logging (off unless KTRADE_REGIME_MODE set)
        self._seed_price_references_from_data(data_map)   # v10.6: seed prior-close refs
        self._roll_trading_day_if_needed(equity)          # v10.7: daily reset

        # 0a0. v10.7: hard stop if the persistent kill switch is active
        if self.emergency is not None and self.emergency.active():
            log.critical("Kill switch ACTIVE (%s) -- no trading this cycle", self.emergency.reason)
            results["blocked"].append({"ticker": "*", "reason": f"kill switch: {self.emergency.reason}"})
            return results

        # 0a. Check loss guard against REAL equity
        if self.loss_guard.check(equity):
            log.critical("Daily loss guard triggered â€” skipping cycle")
            return results

        # 0b. Cost check
        if not self.cost.budget_ok():
            log.warning(f"Budget limit â€” skipping. {self.cost.status()}")
            return results

        # 1. Monitor existing positions
        exits = self.execution.monitor.check(prices)
        results["exits"] = exits

        # 1b. v10.9 earnings-ahead exits — trim/flatten holdings before a binary
        #     earnings event (the "exit ahead of MU earnings" behavior).
        results["earnings_exits"] = self._earnings_blackout_exits()

        # 2. Research â€” rank universe
        interval = os.getenv("KTRADE_SCAN_INTERVAL", "1d").strip() or "1d"
        top_scores = self.research.run(data_map, interval)
        # v10.3: count one API call per ticker, not one total
        self.cost.record_calls(count=len(data_map), cost=0.01 * len(data_map))

        # 3. For each top pick: strategy â†’ risk â†’ execute
        placed = 0
        for score in top_scores[:10]:  # evaluate top 10
            if placed >= CFG.max_positions:
                break
            intraday = interval.lower() in INTRADAY_INTERVALS
            strat_signals = self.strategy.run(score.ticker, data_map[score.ticker], intraday=intraday)

            # v10.3: the strategy that DROVE the score must itself fire.
            # Map the scorer's label to a StrategyAgent key.
            label_to_key = {"MACD_EMA": "macd", "ORB": "orb", "ORB_VWAP": "orb_vwap", "MOMENTUM": "momentum", "TREND": "trend", "TREND_CONTINUATION": "trend_continuation", "VWAP_RECLAIM": "vwap_reclaim"}
            need = label_to_key.get(score.strategy, "momentum")
            if strat_signals.get(need, 0) != 1:
                results["blocked"].append({"ticker": score.ticker,
                    "reason": f"scored {score.strategy} but {need} signal did not confirm"})
                continue

            # v10.3: RiskEngine is the sole sizer -- send a risk budget, not a qty.
            risk_budget = (equity or CFG.account_value) * CFG.risk_per_trade   # v10.6: real equity
            trade = TradeRequest(
                ticker=score.ticker, side="buy", qty=0,
                price=score.price, conviction=int(score.score),
                strategy=score.strategy, atr=score.atr,
                desired_risk_dollars=risk_budget,
            )
            decision = self.risk.evaluate(trade)

            # v13.0: copilot advisory layer. On a rule-approved BUY, get an LLM
            # second opinion, LOG it to the audit ledger, and — only in 'active'
            # mode — let a SKIP/HOLD veto the entry. 'shadow'/'off' never change
            # what trades; an ABSTAIN (LLM down / unparseable) always fails open.
            if decision.approved and self.copilot.mode != "off":
                held = set(getattr(self.risk.engine, "open_positions", {}).keys())
                verdict = self.copilot.consult(score, decision, market=MARKET, held=held)
                self.copilot.ledger.record(score, decision, verdict, mode=self.copilot.mode)
                if verdict.disagrees_with_buy:
                    log.info("COPILOT disagrees on %s: %s (%s) [mode=%s]",
                             score.ticker, verdict.verdict, verdict.reason, self.copilot.mode)
                    results.setdefault("copilot_disagreements", []).append(
                        {"ticker": score.ticker, "verdict": verdict.verdict, "reason": verdict.reason})
                if self.copilot.should_block(verdict):
                    log.warning("COPILOT VETO %s (%s): %s", score.ticker, verdict.verdict, verdict.reason)
                    results["blocked"].append({"ticker": score.ticker,
                        "reason": f"copilot veto ({verdict.verdict}): {verdict.reason}"})
                    continue

            if decision.approved:
                fill = self.execution.execute(decision, score)
                if fill.get("filled"):
                    # Only record a fill the broker actually confirmed. v10.3.
                    self.risk.record_fill(score.ticker, "buy", fill["qty"], fill["price"])
                    results["trades"].append({"ticker": score.ticker, "qty": fill["qty"],
                        "price": fill["price"], "stop": decision.stop_price, "target": decision.target_price})
                    placed += 1
                else:
                    results["blocked"].append({"ticker": score.ticker,
                        "reason": f"approved but not filled: {fill.get('reason')}"})
            else:
                results["blocked"].append({"ticker": score.ticker, "reason": decision.reason})

        log.info(f"Cycle {self._cycle}: {placed} trades placed, {len(results['blocked'])} blocked, {len(exits)} exits")
        return results

    def ask(self, question: str, data_map: Dict[str, pd.DataFrame] = None) -> str:
        """Natural language query â€” uses Claude API."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return "ANTHROPIC_API_KEY is not configured in the project .env file."
        try:
            import requests
            top = self.research.run(data_map) if data_map else []
            context = f"""You are KTrade AI advisor (v9.0).
Current market: VIX={MARKET.vix:.1f} | Crash risk={MARKET.crash_risk_score:.0f}/100
Risk engine: {self.risk.engine.status()}
Top conviction tickers: {[(s.ticker, s.score) for s in top[:5]]}
"""
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type":"application/json",
                         "x-api-key": api_key,
                         "anthropic-version": "2023-06-01"},
                json={"model":"claude-sonnet-4-6","max_tokens":500,
                      "system":context,"messages":[{"role":"user","content":question}]},
                timeout=30)
            r.raise_for_status()
            return r.json().get("content",[{}])[0].get("text","No response")
        except Exception as e:
            return f"Error: {e}"

    def _refresh_market_state(self, data_map):
        """v10.3: refresh live market state. VIX needs a real feed (market_fn);
        flash-crash is derived from SPY in data_map when available. If neither
        is present the state is STALE and we log it rather than trust 18.0."""
        updated = False
        if self.market_fn is not None:
            try:
                m = self.market_fn() or {}
                if m.get("vix"):        MARKET.vix = float(m["vix"]); MARKET.vix_updated_at = datetime.now().isoformat(); updated = True
                if m.get("spy_price"):  MARKET.spy_price = float(m["spy_price"]); updated = True
                if "flash_crash" in m:  MARKET.flash_crash_active = bool(m["flash_crash"]); updated = True
            except Exception as e:
                log.error(f"market_fn failed: {e}")
        # Derive an intraday flash-crash flag from SPY bars if we have them.
        spy = (data_map or {}).get("SPY")
        if spy is not None and len(spy) >= 2:
            drop = (spy["close"].iloc[-1] / spy["close"].iloc[-2] - 1) * 100
            if drop <= -CFG.flash_crash_drop_pct:
                MARKET.flash_crash_active = True
                log.critical(f"ð¨ Flash-crash flag: SPY {drop:.1f}% vs prior bar")
                updated = True
        MARKET.last_updated = datetime.now().isoformat()
        if not updated:
            log.warning("Market state STALE -- no live feed wired (VIX=%.1f assumed). "
                        "Pass market_fn to KTradeCEO for live VIX/SPY.", MARKET.vix)

    def _seed_price_references_from_data(self, data_map: dict) -> None:
        """v10.6: seed RiskEngine price references from each ticker's prior
        close so the bad-tick gate has a reference for every scanned name."""
        refs = {}
        ranges = {}
        for ticker, df in (data_map or {}).items():
            try:
                if df is not None and len(df) >= 2:
                    refs[ticker] = float(df["close"].iloc[-2])
            except Exception:
                pass
            try:
                rng = _intraday_range_pct(df)
                if rng > 0:
                    ranges[ticker] = rng
            except Exception:
                pass
        if refs:
            self.risk.engine.seed_references(refs)
        if ranges:
            self.risk.engine.seed_intraday_ranges(ranges)

    def _log_regime_shadow(self, data_map):
        """v13.3: SHADOW volatility-regime logging. Computes the current regime from
        the benchmark series and appends it to the regime ledger. Changes NOTHING
        about trading. Off unless KTRADE_REGIME_MODE != 'off'. Fully guarded — never
        raises into the cycle."""
        if os.getenv("KTRADE_REGIME_MODE", "off").strip().lower() == "off":
            return
        try:
            try:
                from agent.regime_estimator import RegimeEstimator, RegimeLedger
            except Exception:
                import sys as _sys
                _here = os.path.dirname(os.path.abspath(__file__))
                if _here not in _sys.path:
                    _sys.path.insert(0, _here)
                from regime_estimator import RegimeEstimator, RegimeLedger
            bench = os.getenv("KTRADE_REGIME_BENCHMARK", "SPY").upper()
            df = (data_map or {}).get(bench)
            cols = getattr(df, "columns", [])
            if df is None or "close" not in list(cols):
                return
            closes = [float(x) for x in df["close"].tolist()]
            if len(closes) < 30:
                return
            snap = RegimeEstimator().current(closes)
            RegimeLedger().record(snap, benchmark=bench)
            log.info("REGIME shadow: %s vol=%s P(escalate)=%s",
                     snap.get("regime"), snap.get("realized_vol"), snap.get("escalation_prob"))
        except Exception as exc:
            log.debug("regime shadow log skipped: %s", exc)

    def _broker_truth_guard(self):
        """v12.8: before each autonomous cycle, verify the broker is reachable and
        that its positions match our internal view. Returns (safe_to_trade, reason).

        - Broker unreachable for KTRADE_MAX_BROKER_FAILS consecutive cycles while we
          hold open risk -> halt new entries (server-side brackets still protect the
          open positions).
        - Position desync (we think we hold a name the broker does not) -> halt new
          entries and fire the kill switch (cancel orders; flatten only if
          KTRADE_DESYNC_AUTO_FLATTEN=true). Closes the manual desync->kill gap so the
          autonomous loop no longer trades on phantom positions.
        """
        # v12.8: regime conservatism — if blind to VIX/regime (no fresh feed),
        # refuse new entries by default. This makes the previously-dormant VIX
        # ladder fail safe instead of trusting the calm 18.0 default, without
        # coupling regime policy into the per-trade RiskEngine primitive.
        if _vix_is_stale() and os.getenv("KTRADE_REQUIRE_FRESH_VIX", "true").lower() == "true":
            return False, ("REGIME STALE — no fresh VIX feed; conservative mode, no new entries "
                           "(wire market_fn, or set KTRADE_REQUIRE_FRESH_VIX=false)")
        if self.broker is None:
            return True, "no broker (paper/demo)"
        try:
            broker_positions = self.broker.get_positions() or []
            self._broker_fail_streak = 0
        except Exception as exc:
            self._broker_fail_streak += 1
            have_risk = bool(getattr(self.risk.engine, "open_positions", {}))
            max_fail = int(os.getenv("KTRADE_MAX_BROKER_FAILS", "3"))
            if have_risk and self._broker_fail_streak >= max_fail:
                log.critical("Broker unreachable %d cycles while holding risk — HALTING new entries: %s",
                             self._broker_fail_streak, exc)
                return False, f"broker unreachable x{self._broker_fail_streak}"
            log.warning("Broker truth check failed (%d/%s); skipping cycle to be safe: %s",
                        self._broker_fail_streak, max_fail, exc)
            return False, "broker truth unavailable"
        broker_syms = {str((p.get("symbol") or p.get("ticker") or "")).upper()
                       for p in broker_positions}
        internal_syms = {str(s).upper()
                         for s in getattr(self.risk.engine, "open_positions", {}).keys()}
        phantom = internal_syms - broker_syms
        if phantom:
            log.critical("DESYNC: agent holds %s not present at broker — HALTING new entries",
                         sorted(phantom))
            if self.emergency is not None:
                try:
                    flatten = os.getenv("KTRADE_DESYNC_AUTO_FLATTEN", "false").lower() == "true"
                    self.emergency.trigger(f"broker desync: phantom {sorted(phantom)}", flatten=flatten)
                except Exception as exc:
                    log.error("desync emergency trigger failed: %s", exc)
            return False, f"position desync: phantom {sorted(phantom)}"
        return True, "in sync"

    def run_autonomous(self, data_fn=None):
        """Full autonomous loop with market-phase-aware scheduling.

        v12 cleanup: if no data_fn is supplied, use the same real/default market
        feed path as --once instead of running empty cycles. Each cycle is
        wrapped in a fail-safe try/except so a transient data/API failure does
        not crash the process or create phantom decisions.
        """
        self._running = True
        if data_fn is None:
            data_fn = default_cycle_data_fn
            log.info("No data_fn supplied; using default PolygonDataFeed batch loader")
        log.info("ðŸš€ KTrade CEO autonomous mode started")
        while self._running:
            try:
                if self.heartbeat.is_market_open():
                    data_map = data_fn() or {}
                    if not data_map:
                        log.warning("Autonomous cycle skipped: no market data returned")
                    else:
                        prices = {}
                        for t, df in data_map.items():
                            try:
                                if df is not None and len(df) > 0:
                                    prices[t] = float(df["close"].iloc[-1])
                            except Exception as exc:
                                log.warning("Price extraction skipped for %s: %s", t, exc)
                        safe, reason = self._broker_truth_guard()
                        if safe:
                            self.run_cycle(data_map, prices)
                        else:
                            log.critical("Autonomous cycle SKIPPED (no new entries) — %s", reason)
                else:
                    log.info(f"Market closed ({self.heartbeat.get_phase().value}) â€” sleeping")
            except Exception as exc:
                log.exception("Autonomous cycle failed safely; no orders submitted from failed cycle: %s", exc)
            time.sleep(self.heartbeat.scan_interval())


# ===========================================================================
# SECTION 13 â€” BRACKET ORDER BUILDER (for Alpaca)
# ===========================================================================
def build_bracket_order(ticker: str, qty: float, side: str,
                         stop: float, target: float) -> dict:
    """Sends stop + target server-side to Alpaca. AI not needed to exit."""
    return {
        "symbol": ticker, "qty": str(int(qty)),
        "side": side, "type": "market", "time_in_force": "day",
        # v10.1: traceable client_order_id for intent <-> fill mapping
        "client_order_id": f"ktrade-{ticker}-{side}-{__import__('uuid').uuid4().hex[:12]}",
        "order_class": "bracket",
        "stop_loss":   {"stop_price":   str(round(stop, 2))},
        "take_profit": {"limit_price":  str(round(target, 2))},
    }


# ===========================================================================
# DEFAULT LIVE/DATA LOOP LOADER
# ===========================================================================
def default_cycle_data_fn() -> Dict[str, pd.DataFrame]:
    """Default data loader for --once and no-flag autonomous mode.

    Uses the configured data provider chain in PolygonDataFeed and returns a
    dict[ticker, DataFrame]. This prevents autonomous mode from silently running
    empty cycles when the caller forgets to pass data_fn.
    """
    if str(project_dir) not in sys.path:
        sys.path.insert(0, str(project_dir))
    from data.ktrade_data import PolygonDataFeed
    interval = os.getenv("KTRADE_SCAN_INTERVAL", "1d").strip() or "1d"
    days = int(os.getenv("KTRADE_SCAN_DAYS", "320"))
    symbols = _scan_symbols()
    feed = PolygonDataFeed()
    return feed.batch_get(symbols, days=days, interval=interval)

# ===========================================================================
# REAL-DATA SCORE-ONLY RUNNER
# ===========================================================================
CORE_SCAN_SYMBOLS = ["SPY", "QQQ", "IWM", "GLD", "TLT", "NVDA", "MSFT", "AAPL"]

EXTENDED_SCAN_SYMBOLS = [
    "SPY", "QQQ", "IWM", "GLD", "TLT", "NVDA", "MSFT", "AAPL",
    "CEG", "VST", "GEV", "ETN", "PWR", "VRT", "MOD", "MPWR", "NVTS", "TLN",
    "SOXX", "XAR", "IDGT", "QTUM", "DRAM", "MRVL", "MU", "RMBS", "LEU",
    "RGTI", "QBTS", "IONQ", "TQQQ", "NVAX", "AMD", "INTC", "CRDO", "PL",
    "NOK", "ARM", "NBIS", "QCOM", "MSTR", "SMCI", "IREN", "CRWV", "RKLB",
    "IRDM", "KTOS", "DXYZ", "BTC-USD", "ETH-USD",
]

POLYGON_TICKER_ALIASES = {
    "BTC-USD": "X:BTCUSD",
    "ETH-USD": "X:ETHUSD",
}


def _scan_symbols() -> list[str]:
    raw = os.getenv("KTRADE_SCAN_SYMBOLS", "").strip()
    if raw:
        return [item.strip().upper() for item in raw.split(",") if item.strip()]

    universe = os.getenv("KTRADE_SCAN_UNIVERSE", "core").strip().lower()
    if universe in {"extended", "all"}:
        return EXTENDED_SCAN_SYMBOLS
    return CORE_SCAN_SYMBOLS

INTRADAY_STRATEGIES = {"ORB", "ORB_VWAP", "VWAP_RECLAIM", "RSI_REVERSAL", "PREV_BAR_BREAKOUT", "VWAP_PULLBACK"}
INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "1h"}


def classify_trade_metadata(strategy: str, interval: str) -> dict:
    """Return trade metadata for frontend display. Backend is the source of truth."""
    strategy_key = (strategy or "").upper()
    interval_key = (interval or "1d").lower()
    if interval_key in INTRADAY_INTERVALS or strategy_key in INTRADAY_STRATEGIES:
        return {
            "trade_type": "INTRADAY",
            "timeframe": interval_key.upper(),
            "holding_period": "same day",
            "exit_rule": "Exit before market close or when target/stop is hit",
        }
    if strategy_key in {"MOMENTUM", "TREND_CONTINUATION"}:
        return {
            "trade_type": "MOMENTUM",
            "timeframe": interval_key.upper(),
            "holding_period": "2 days to 3 weeks",
            "exit_rule": "Hold while trend remains valid; exit on stop or trend break",
        }
    return {
        "trade_type": "SWING",
        "timeframe": interval_key.upper(),
        "holding_period": "2 days to 10 days",
        "exit_rule": "Exit on target, stop, or signal invalidation",
    }
def _price_validation(ticker: str, scanner_price: float, reference_price: Optional[float], max_diff_pct: float) -> dict:
    """Compare scanner close/last price with broker reference price.
    If Alpaca is unavailable, return a warning instead of pretending validation happened.
    """
    out = {
        "ok": True,
        "scanner_price": round(float(scanner_price or 0), 4),
        "reference_price": None,
        "difference_pct": None,
        "source": "alpaca_snapshot",
        "reason": "not_checked",
    }
    if reference_price is None or float(reference_price or 0) <= 0:
        out["reason"] = "alpaca_reference_unavailable"
        return out
    ref = float(reference_price)
    px = float(scanner_price or 0)
    diff_pct = abs(px - ref) / ref * 100 if ref else 0.0
    out.update({"reference_price": round(ref, 4), "difference_pct": round(diff_pct, 3), "reason": "ok"})
    if diff_pct > max_diff_pct * 100:
        out["ok"] = False
        out["reason"] = f"scanner_price_diff>{max_diff_pct*100:.1f}%"
    return out


def _fetch_symbol_frame(feed, symbol: str, scan_interval: str):
    polygon_symbol = POLYGON_TICKER_ALIASES.get(symbol, symbol)
    frame = feed.get_bars(polygon_symbol, days=320, interval=scan_interval)
    return symbol, frame


def run_polygon_score_only() -> list[ConvictionScore]:
    """Run this file's scorer against the configured market-data feed. Never submits orders."""
    if str(project_dir) not in sys.path:
        sys.path.insert(0, str(project_dir))
    from data.ktrade_data import PolygonDataFeed

    symbols = _scan_symbols()
    n_approved = sum(len(v) for v in APPROVED_PARAMS.values()) + sum(len(v) for v in APPROVED_INTRADAY_PARAMS.values())
    if n_approved:
        log.info(f"Using VectorBT approved params for {n_approved} ticker/strategy combos")
    else:
        log.info("No approved params -- run ktrade_vectorbt.py and/or ktrade_intraday_vectorbt.py for better signals")
    feed = PolygonDataFeed()

    scorer = ConvictionScorer()
    results = []
    errors = []
    scan_interval = os.getenv("KTRADE_SCAN_INTERVAL", "1d").strip() or "1d"
    workers = max(1, int(os.getenv("KTRADE_SCAN_WORKERS", "1")))

    frames: Dict[str, pd.DataFrame] = {}
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_fetch_symbol_frame, feed, symbol, scan_interval) for symbol in symbols]
            for fut in as_completed(futs):
                try:
                    symbol, frame = fut.result()
                    if frame is None or len(frame) < 50:
                        errors.append(f"{symbol}: insufficient market-data history")
                    else:
                        frames[symbol] = frame
                except Exception as exc:
                    errors.append(f"worker fetch failed: {exc}")
    else:
        for index, symbol in enumerate(symbols):
            try:
                symbol, frame = _fetch_symbol_frame(feed, symbol, scan_interval)
                if frame is None or len(frame) < 50:
                    errors.append(f"{symbol}: insufficient market-data history")
                else:
                    frames[symbol] = frame
            except Exception as exc:
                errors.append(f"{symbol}: fetch failed: {exc}")
            if index < len(symbols) - 1:
                time.sleep(float(os.getenv("POLYGON_SCAN_DELAY_SECONDS", "1.0")))

    stock_symbols = [s for s in symbols if "-" not in s and not s.startswith("X:")]
    try:
        reference_prices = feed.get_alpaca_reference_prices(stock_symbols)
    except AttributeError:
        reference_prices = {}
    except Exception as exc:
        log.warning(f"Alpaca price-reference validation failed: {exc}")
        reference_prices = {}

    max_diff_pct = float(os.getenv("KTRADE_MAX_PRICE_DIFF_PCT", "5")) / 100.0
    for item in scorer.rank_universe(frames, scan_interval):
        validation = _price_validation(item.ticker, item.price, reference_prices.get(item.ticker), max_diff_pct)
        item.price_validation = validation
        if not validation["ok"]:
            item.signal = 0
            errors.append(f"{item.ticker}: blocked BUY due to price validation ({validation['reason']}; scan={validation['scanner_price']} ref={validation['reference_price']})")
        results.append(item)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "source": "Market data: Polygon -> Alpaca -> Finnhub -> yfinance fallback; Alpaca reference validation for BUY signals when credentials exist",
        "orders_submitted": False,
        "universe": os.getenv("KTRADE_SCAN_UNIVERSE", "core"),
        "symbols_requested": symbols,
        "minimum_conviction": CFG.min_conviction_score,
        "scan_interval": scan_interval,
        "price_validation_max_diff_pct": max_diff_pct * 100,
        "errors": errors,
        "results": [
            {
                "ticker": item.ticker,
                "action": "BUY" if item.signal == 1 else "WATCH",
                "conviction": item.score,
                "price": float(item.price),
                "strategy": item.strategy,
                **classify_trade_metadata(item.strategy, scan_interval),
                "atr": item.atr,
                "price_validation": getattr(item, "price_validation", None),
                "blocked_reason": None if item.signal == 1 else (getattr(item, "price_validation", {}) or {}).get("reason"),
                "components": {
                    key: round(float(value), 1)
                    for key, value in item.components.items()
                },
            }
            for item in sorted(results, key=lambda value: value.score, reverse=True)
        ],
    }
    output_path = project_dir / "data" / "ktrade_scan_latest.json"
    # v12.8: atomic write (temp + rename) so a concurrent backend read can never
    # observe a half-written scan file (TOCTOU / partial-write integrity).
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _tmp = output_path.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(_tmp, output_path)

    print("\nKTrade read-only market-data scan")
    print("No orders are submitted.")
    print(f"Minimum BUY conviction: {CFG.min_conviction_score}")
    print(f"Price validation max diff: {max_diff_pct*100:.1f}%\n")
    for item in payload["results"]:
        print(
            f"{item['ticker']:5} {item['action']:5} "
            f"conviction={item['conviction']:5.1f} "
            f"price=${item['price']:,.2f} strategy={item['strategy']} type={item.get('trade_type', '-')} tf={item.get('timeframe', '-')}"
        )
        pv = item.get("price_validation") or {}
        if pv.get("reference_price") is not None:
            print(f"      price check: scan={pv['scanner_price']} alpaca={pv['reference_price']} diff={pv['difference_pct']}% {pv['reason']}")
        elif pv:
            print(f"      price check: {pv.get('reason')}")
        print(
            "      "
            + " | ".join(
                f"{key}={value:.1f}" for key, value in item["components"].items()
            )
        )
    for error in errors:
        print(f"ERROR {error}")
    print(f"\nSaved: {output_path}")
    return results


# ===========================================================================
# MAIN CLI
# ===========================================================================
def main():
    parser = argparse.ArgumentParser(description=f"KTrade PRO Unified Agent v{__version__}")
    parser.add_argument("--version", action="version", version=f"KTrade PRO Unified Agent v{__version__} ({__updated__})")
    parser.add_argument("--once",        action="store_true", help="Run one cycle and exit")
    parser.add_argument("--score-only",  action="store_true", help="Score tickers only, no trades")
    parser.add_argument("--validate",    action="store_true", help="Backtest all strategies")
    parser.add_argument("--crash-check", action="store_true", help="Run crash detection only")
    parser.add_argument("--ask",         type=str,            help="Ask a natural language question")
    parser.add_argument("--risk-status", action="store_true", help="Show risk engine status")
    args = parser.parse_args()

    print(f"\n{'='*54}")
    print(f"  KTrade PRO â€” Unified Agent v{__version__}")
    print(f"  {__updated__} | iT LLC")
    print(f"  Mode: {'LIVE' if CFG.live_trading else 'PAPER'}")
    print(f"{'='*54}\n")

    ceo = KTradeCEO()

    if args.risk_status:
        print(json.dumps(ceo.risk.engine.status(), indent=2))
        return

    if args.crash_check:
        result = ceo.crash_det.evaluate(
            vix=MARKET.vix, put_call_ratio=MARKET.put_call_ratio,
            spy_5d_return=-1.5, advance_decline=MARKET.advance_decline
        )
        print(f"\nCrash Risk Score: {result['score']}/100 ({result['risk_level']})")
        print(f"Signals: {', '.join(result['signals']) or 'None'}")
        if result['puts']:
            print(f"Recommended puts: {result['puts']}")
        return

    if args.ask:
        print(f"\nðŸ¤– {ceo.ask(args.ask)}")
        return

    if args.score_only:
        print("Score-only mode â€” no trades will be placed")
        run_polygon_score_only()
        return

    if args.validate:
        if not MASTER_AVAILABLE:
            print(
                "Validation is unavailable: trading_agent_master.py is not "
                "present in this KTrade v9 folder."
            )
            return
        print(
            "Validation requires the legacy master backtest configuration; "
            "no default validation dataset is configured."
        )
        return

    if args.once:
        print("Single cycle mode")
        interval = os.getenv("KTRADE_SCAN_INTERVAL", "1d").strip() or "1d"
        data_map = default_cycle_data_fn()
        prices = {t: float(df["close"].iloc[-1]) for t, df in data_map.items()
                  if df is not None and len(df) > 0}
        refs = {t: float(df["close"].iloc[-2]) for t, df in data_map.items()
                if df is not None and len(df) >= 2}
        ceo.risk.engine.seed_references(refs)
        print(f"Loaded {len(data_map)} symbols ({interval}); running one real cycle...")
        result = ceo.run_cycle(data_map, prices)
        print(f"Cycle result: {len(result.get('trades',[]))} trades, "
              f"{len(result.get('blocked',[]))} blocked, {len(result.get('exits',[]))} exits")
        return

    # Full autonomous loop (v12: uses default real data loader when no data_fn is passed)
    ceo.run_autonomous()


if __name__ == "__main__":
    main()






