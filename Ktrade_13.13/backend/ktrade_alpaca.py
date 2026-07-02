"""
KTrade PRO â€” Alpaca Paper Trading Bridge
==========================================
Connects KTrade frontend to Alpaca paper trading account.
Fetches real positions, live prices, account info.
Places paper trades directly from the dashboard.

SETUP (one time):
  1. Go to https://alpaca.markets â†’ Sign up free
  2. Switch to Paper Trading environment
  3. Copy your Paper API Key + Secret
  4. pip install alpaca-trade-api flask flask-cors websocket-client

RUN:
  export ALPACA_KEY="PKxxxxxxxxxxxxxxxx"
  export ALPACA_SECRET="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  python ktrade_alpaca.py

THEN open KTrade â†’ it pulls REAL Alpaca paper data
"""

import os, sys, time, json, threading, logging, uuid
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# v12.2: KTrade-only local runtime support. Loads optional project .env and
# app-data .env, and keeps logs/db outside the source folder when configured.
try:
    from ktrade_runtime.paths import load_ktrade_env, logs_dir, runtime_status
    load_ktrade_env()
    _backend_log_file = logs_dir() / "ktrade_backend.log"
except Exception:
    runtime_status = None  # type: ignore
    _backend_log_file = PROJECT_DIR / "ktrade_backend.log"
    try:
        for key, value in dotenv_values(PROJECT_DIR / ".env", encoding="utf-8-sig").items():
            if value is not None:
                os.environ.setdefault(key, str(value).strip())
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(_backend_log_file, mode="a")],
)
log = logging.getLogger("KTrade")

from broker_reconciler import BrokerReconciler  # v10.1 broker-truth reconcile
from threading import RLock as _RLock

# v11.1: paper->live promotion gate (records paper results, gates live orders).
# Defensive import — if the module is absent the hooks become no-ops.
try:
    from risk.promotion_gate import (
        block_reason_if_live as _promo_block,
        record_truth_trades as _promo_record_trades,
    )
except Exception:
    try:
        from promotion_gate import (
            block_reason_if_live as _promo_block,
            record_truth_trades as _promo_record_trades,
        )
    except Exception:
        def _promo_block(_symbol):  # type: ignore
            return None
        def _promo_record_trades(_trades):  # type: ignore
            return 0

# v11.2: regime strategy-switch (defensive import; baseline no-op if absent).
try:
    from data.strategy_selector import active_profile as _active_strategy_profile
except Exception:
    try:
        from strategy_selector import active_profile as _active_strategy_profile
    except Exception:
        class _BaselineProfile:  # mirrors StrategyProfile's consumed fields
            name = "balanced"; regime = "NEUTRAL"; size_mult = 1.0; allow_new_longs = True
            def __init__(self, base): self.min_conviction = base
        def _active_strategy_profile(base):  # type: ignore
            return _BaselineProfile(base)

# v11.3 (Milestone 1): portfolio source-of-truth gate + immutable audit log.
try:
    from risk.portfolio_context import get_portfolio as _get_portfolio
    from risk.audit_log import record as _audit_record
except Exception:
    try:
        from portfolio_context import get_portfolio as _get_portfolio
        from audit_log import record as _audit_record
    except Exception:
        _get_portfolio = None
        def _audit_record(_event, _payload):  # type: ignore
            return None


def _portfolio_gate_enabled_env() -> bool:
    return os.getenv("KTRADE_PORTFOLIO_GATE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _portfolio_cycle_gate():
    """Portfolio-level kill switch (net-worth drawdown + feed staleness).

    If the portfolio gate is disabled, this is a no-op. If it is enabled, any
    exception/missing module fails SAFE and blocks new risk. This is important
    because the optional portfolio gate is the source-of-truth for total capital once enabled.
    """
    if _get_portfolio is None:
        if _portfolio_gate_enabled_env():
            return False, "portfolio gate enabled but module absent"
        return True, "portfolio module absent; gate disabled"
    try:
        return _get_portfolio().cycle_gate()
    except Exception as exc:
        if _portfolio_gate_enabled_env():
            return False, f"portfolio gate error: {exc}"
        return True, f"portfolio gate error ignored because gate disabled: {exc}"


def _portfolio_exposure_ok(intended_notional, deployed_notional):
    if _get_portfolio is None:
        if _portfolio_gate_enabled_env():
            return False, "portfolio gate enabled but module absent"
        return True, "portfolio module absent; gate disabled"
    try:
        return _get_portfolio().exposure_ok(intended_notional, deployed_notional)
    except Exception as exc:
        if _portfolio_gate_enabled_env():
            return False, f"portfolio exposure gate error: {exc}"
        return True, f"portfolio exposure error ignored because gate disabled: {exc}"


def _current_deployed_notional():
    """Best-effort sum of open-position market values (the trading sub-account's
    deployed capital). 0.0 if positions are unavailable."""
    try:
        return float(sum(float(p.get("marketValue", 0) or 0) for p in (fetch_positions() or [])))
    except Exception:
        return 0.0


def _audit(event, payload):
    try:
        _audit_record(event, payload)
    except Exception:
        pass
STATE_LOCK = _RLock()  # v10.7: guards the shared `state` dict
def update_state(**updates):
    with STATE_LOCK:
        state.update(updates)
        state["last_updated"] = datetime.now().isoformat()
def snapshot_state():
    with STATE_LOCK:
        return json.loads(json.dumps(state, default=str))

def append_error(message):
    with STATE_LOCK:
        state.setdefault("errors", []).append({"time": datetime.now().isoformat(), "msg": str(message)})
        state["errors"] = state.get("errors", [])[-100:]
        state["last_updated"] = datetime.now().isoformat()

try:
    from data.openai_finbert_sentiment import analyze_ticker_sentiment
except Exception:
    analyze_ticker_sentiment = None

# â”€â”€ Alpaca credentials (paper trading) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
API_KEY    = os.environ.get("ALPACA_KEY", "")
API_SECRET = os.environ.get("ALPACA_SECRET", "")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "") or os.environ.get("FINNHUB_KEY", "")
FINNHUB_BASE = "https://finnhub.io/api/v1"
ORDER_SUBMISSION_ENABLED = (
    os.environ.get("KTRADE_PAPER_ORDER_SUBMISSION", "false").lower() == "true"
)

# Alpaca paper trading endpoints
BASE_URL   = "https://paper-api.alpaca.markets"      # paper trading
DATA_URL   = "https://data.alpaca.markets"            # market data
STREAM_URL = "wss://stream.data.alpaca.markets/v2/iex"  # real-time quotes

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type":        "application/json"
}

# â”€â”€ Try importing dependencies â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    import requests as req
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False
    log.error("requests not installed: pip install requests")

try:
    import websocket
    WS_OK = True
except ImportError:
    WS_OK = False
    log.warning("websocket-client not installed: pip install websocket-client (optional for streaming)")

# â”€â”€ Demo fallback â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Fresh demo/virtual account defaults.
# Note: when Alpaca paper is connected, /account, /positions and /orders come from Alpaca.
# These values are used only when backend is offline/demo mode.
DEMO_POSITIONS = []
DEMO_PRICES  = {"NVDA":131.2,"TSLA":228.4,"AAPL":195.3,"MSFT":412.8,"GOOGL":172.5,"QQQ":478.6,"QNT":100.0,"ORCL":155.2,"IBM":208.4}
DEMO_ACCOUNT = {"equity":100000.0,"cash":100000.0,"buying_power":100000.0,"portfolio_value":100000.0,"daytrade_count":0,"status":"ACTIVE","mode":"DEMO"}

# â”€â”€ Live state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
state = {
    "connected": False,
    "mode":      "demo",
    "positions": DEMO_POSITIONS,
    "prices":    DEMO_PRICES.copy(),
    "account":   DEMO_ACCOUNT.copy(),
    "orders":    [],
    "last_updated": datetime.now().isoformat(),
    "errors":    [],
    "reconciliation": {
        "ok": True,
        "status": "DEMO",
        "source_of_truth": "alpaca",
        "checked_at": datetime.now().isoformat(),
        "broker_positions": len(DEMO_POSITIONS),
        "dashboard_positions_before_sync": len(DEMO_POSITIONS),
        "open_orders": 0,
        "recent_filled_orders": 0,
        "broker_symbols": sorted([p["ticker"] for p in DEMO_POSITIONS]),
        "dashboard_symbols_before_sync": sorted([p["ticker"] for p in DEMO_POSITIONS]),
        "warnings": []
    }
}

# â”€â”€ Alpaca API helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def alpaca_get(path, base=BASE_URL):
    if not REQUESTS_OK or not API_KEY:
        return None
    try:
        r = req.get(f"{base}{path}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"GET {path} failed: {e}")
        append_error(e)
        return None

def alpaca_post(path, body):
    if not REQUESTS_OK or not API_KEY:
        return None
    try:
        r = req.post(f"{BASE_URL}{path}", headers=HEADERS, json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"POST {path} failed: {e}")
        return None

def alpaca_delete(path):
    if not REQUESTS_OK or not API_KEY:
        return None
    try:
        r = req.delete(f"{BASE_URL}{path}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"DELETE {path} failed: {e}")
        return None


def finnhub_get(path, params=None):
    """Optional Finnhub helper for quote/news fallback data."""
    if not REQUESTS_OK or not FINNHUB_KEY:
        return None
    query = dict(params or {})
    query["token"] = FINNHUB_KEY
    try:
        r = req.get(f"{FINNHUB_BASE}{path}", params=query, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Finnhub GET {path} failed: {e}")
        return None

# â”€â”€ Fetch account â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fetch_account():
    data = alpaca_get("/v2/account")
    if not data:
        return None
    return {
        "equity":          float(data.get("equity", 0)),
        "cash":            float(data.get("cash", 0)),
        "buying_power":    float(data.get("buying_power", 0)),
        "portfolio_value": float(data.get("portfolio_value", 0)),
        "daytrade_count":  int(data.get("daytrade_count", 0)),
        "status":          data.get("status", "UNKNOWN"),
        "mode":            "paper"
    }

# â”€â”€ Fetch positions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fetch_positions():
    data = alpaca_get("/v2/positions")
    if data is None:
        return []

    positions = []
    for p in data:
        positions.append({
            "ticker":        p.get("symbol", ""),
            "name":          p.get("symbol", ""),   # Alpaca doesn't return name in positions
            "shares":        float(p.get("qty", 0)),
            "avgCost":       float(p.get("avg_entry_price", 0)),
            "marketValue":   float(p.get("market_value", 0)),
            "unrealizedPL":  float(p.get("unrealized_pl", 0)),
            "unrealizedPLPC":float(p.get("unrealized_plpc", 0)) * 100,
            "currentPrice":  float(p.get("current_price", 0)),
            "sector":        "Stock"
        })
    return positions

# â”€â”€ Fetch live prices (snapshot) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fetch_prices(tickers):
    if not tickers:
        return {}
    symbols = ",".join(tickers)
    data = alpaca_get(f"/v2/stocks/snapshots?symbols={symbols}", base=DATA_URL)
    if not data:
        return {}

    prices = {}
    for sym, snap in data.items():
        try:
            # latest trade price
            prices[sym] = float(snap.get("latestTrade", {}).get("p", 0)) or \
                          float(snap.get("latestQuote", {}).get("ap", 0))
        except:
            prices[sym] = DEMO_PRICES.get(sym, 0)
    return prices

# â”€â”€ Fetch recent orders â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fetch_orders():
    data = alpaca_get("/v2/orders?status=all&limit=20")
    if not data:
        return []
    orders = []
    for o in data:
        orders.append({
            "id":         o.get("id"),
            "ticker":     o.get("symbol"),
            "side":       o.get("side"),          # buy / sell
            "type":       o.get("type"),          # market / limit
            "qty":        o.get("qty"),
            "filled_qty": o.get("filled_qty"),
            "status":     o.get("status"),        # filled / pending / canceled
            "limit_price":o.get("limit_price"),
            "filled_avg": o.get("filled_avg_price"),
            "created_at": o.get("created_at"),
        })
    return orders

# â”€â”€ Place order â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _position_symbol_map(positions):
    result = {}
    for pos in positions or []:
        symbol = (pos.get("ticker") or pos.get("symbol") or "").upper()
        if symbol:
            result[symbol] = float(pos.get("shares") or pos.get("qty") or 0)
    return result


def reconcile_broker_state(broker_positions, broker_orders, previous_dashboard_positions):
    """Compare dashboard state with Alpaca broker truth."""
    broker_map = _position_symbol_map(broker_positions)
    dashboard_map = _position_symbol_map(previous_dashboard_positions)
    broker_symbols = set(broker_map)
    dashboard_symbols = set(dashboard_map)
    pending_statuses = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "held"}
    open_orders = []
    filled_orders = []
    warnings = []

    for order in broker_orders or []:
        status = str(order.get("status") or "").lower()
        if status in pending_statuses:
            open_orders.append(order)
        if status == "filled":
            filled_orders.append(order)

    removed = sorted(dashboard_symbols - broker_symbols)
    added = sorted(broker_symbols - dashboard_symbols)
    qty_changed = sorted(
        symbol for symbol in broker_symbols & dashboard_symbols
        if abs(float(broker_map.get(symbol, 0)) - float(dashboard_map.get(symbol, 0))) > 0.0001
    )

    if removed:
        warnings.append("Broker is flat for: " + ", ".join(removed))
    if added:
        warnings.append("Broker has new positions: " + ", ".join(added))
    if qty_changed:
        warnings.append("Broker quantity changed: " + ", ".join(qty_changed))
    if open_orders:
        warnings.append(f"{len(open_orders)} open/pending broker order(s)")

    return {
        "ok": len(warnings) == 0,
        "status": "OK" if not warnings else "REVIEW",
        "source_of_truth": "alpaca",
        "checked_at": datetime.now().isoformat(),
        "broker_positions": len(broker_positions or []),
        "dashboard_positions_before_sync": len(previous_dashboard_positions or []),
        "open_orders": len(open_orders),
        "recent_filled_orders": len(filled_orders),
        "broker_symbols": sorted(broker_symbols),
        "dashboard_symbols_before_sync": sorted(dashboard_symbols),
        "warnings": warnings,
    }
# -- v10.1: broker-truth closed trades from REAL fill activities --------------
_RECONCILER = BrokerReconciler(alpaca_get, max_desyncs_before_halt=3)
DESYNC_STATE = {"halt": False, "desyncs": [], "checked_at": None}
SENTIMENT_CACHE = {}
SENTIMENT_TTL_SECONDS = int(os.environ.get("KTRADE_SENTIMENT_TTL_SECONDS", "1800"))


# Dashboard-controlled auto trading (paper only by default).
# The browser only toggles this worker; execution stays in the backend so it
# continues to honor broker/risk/kill-switch checks and does not depend on the
# browser tab staying open.
AUTO_TRADE_LOCK = threading.RLock()
AUTO_TRADE_STATE = {
    "enabled": False,
    "running": False,
    "started_at": None,
    "stopped_at": None,
    "last_cycle_at": None,
    "last_action": None,
    "last_error": None,
    "cycles": 0,
    "orders_submitted": 0,
    "blocked": [],
    "mode": "paper_bracket",
}
AUTO_TRADE_INTERVAL_SECONDS = int(os.environ.get("KTRADE_AUTO_TRADE_INTERVAL_SECONDS", "300"))
AUTO_TRADE_MIN_CONVICTION = float(os.environ.get("KTRADE_AUTO_TRADE_MIN_CONVICTION", "80"))
AUTO_TRADE_QTY = float(os.environ.get("KTRADE_AUTO_TRADE_QTY", "1"))
AUTO_TRADE_MAX_ORDERS_PER_CYCLE = int(os.environ.get("KTRADE_AUTO_TRADE_MAX_ORDERS_PER_CYCLE", "2"))
AUTO_TRADE_STOP_LOSS_PCT = float(os.environ.get("KTRADE_AUTO_TRADE_STOP_LOSS_PCT", "3.0"))
AUTO_TRADE_TAKE_PROFIT_PCT = float(os.environ.get("KTRADE_AUTO_TRADE_TAKE_PROFIT_PCT", "6.0"))
AUTO_TRADE_REQUIRE_SENTIMENT = os.environ.get("KTRADE_AUTO_TRADE_REQUIRE_SENTIMENT", "false").lower() == "true"
AUTO_TRADE_ALLOW_DEMO = os.environ.get("KTRADE_AUTO_TRADE_ALLOW_DEMO", "false").lower() == "true"

def build_truth_closed_trades(after_iso=None, intended_book=None):
    """Verified closed trades + stats built ONLY from real fills (no synthetic
    marks). Pass intended_book {symbol: signed_qty} to flag desyncs."""
    result = _RECONCILER.reconcile(agent_open_book=intended_book, after_iso=after_iso)
    DESYNC_STATE["desyncs"] = [d.to_dict() for d in result.desyncs]
    DESYNC_STATE["checked_at"] = datetime.now().isoformat()
    DESYNC_STATE["halt"] = _RECONCILER.should_halt(result)
    if DESYNC_STATE["halt"]:
        log.critical("DESYNC HALT -- %d position desync(s); book unreliable",
                     result.desync_count)
    _truth_trades = [rt.to_dict() for rt in result.round_trips]
    # v11.1: feed verified closed trades into the promotion ledger (idempotent;
    # no-op unless KTRADE_PROMOTION_ENABLED). Truth source only — same anti-
    # synthetic-mark guarantee as the dashboard win-rate.
    try:
        _promo_record_trades(_truth_trades)
    except Exception:
        pass
    return {
        "trades": _truth_trades,
        "stats": result.stats(),
        "desyncs": DESYNC_STATE["desyncs"],
        "halt": DESYNC_STATE["halt"],
        "meta": {"reconciled": True, "source": "alpaca fill activities",
                 "methodology": "FIFO round-trips from real fills; no synthetic exits"},
    }

def place_order(ticker, qty, side, order_type="market", limit_price=None, client_order_id=None):
    """
    side: 'buy' or 'sell'
    order_type: 'market' or 'limit'
    """
    # v11.1: gate live BUYs through the promotion gate (no-op in paper mode;
    # sells/exits are never blocked).
    if str(side).lower() == "buy":
        _blk = _promo_block(ticker)
        if _blk:
            log.info("[PROMOTION] live BUY blocked for %s — %s", ticker.upper(), _blk)
            return None
    body = {
        "symbol":        ticker.upper(),
        "qty":           str(qty),
        "side":          side,            # "buy" or "sell"
        "type":          order_type,      # "market" or "limit"
        "time_in_force": "day",
        # v10.1: deterministic client_order_id maps intent <-> broker fill
        "client_order_id": client_order_id or f"ktrade-{ticker.upper()}-{side}-{uuid.uuid4().hex[:12]}",
    }
    if order_type == "limit" and limit_price:
        body["limit_price"] = str(limit_price)

    result = alpaca_post("/v2/orders", body)
    return result

# â”€â”€ Cancel order â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def cancel_order(order_id):
    return alpaca_delete(f"/v2/orders/{order_id}")

# v10.7: direct order lookup (await_fill must not rely on the latest-20 list)
def fetch_order(order_id):
    if not order_id:
        return None
    return alpaca_get(f"/v2/orders/{order_id}")

# v10.7: emergency actions used by the kill switch / EmergencyController
def cancel_all_orders():
    """Cancel ALL open orders (Alpaca DELETE /v2/orders)."""
    return alpaca_delete("/v2/orders")

def close_all_positions():
    """Liquidate ALL positions (Alpaca DELETE /v2/positions)."""
    return alpaca_delete("/v2/positions")

# â”€â”€ WebSocket real-time price streaming â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class PriceStreamer:
    def __init__(self, tickers, on_price):
        self.tickers   = tickers
        self.on_price  = on_price
        self.ws        = None
        self.running   = False

    def start(self):
        if not WS_OK or not API_KEY:
            log.info("WebSocket streaming not available (using polling instead)")
            return
        self.running = True
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        def on_open(ws):
            # authenticate
            ws.send(json.dumps({"action":"auth","key":API_KEY,"secret":API_SECRET}))

        def on_message(ws, msg):
            data = json.loads(msg)
            for item in (data if isinstance(data, list) else [data]):
                if item.get("T") == "q":   # quote update
                    sym = item.get("S")
                    ask = item.get("ap", 0)
                    bid = item.get("bp", 0)
                    if sym and (ask or bid):
                        mid = (ask + bid) / 2
                        self.on_price(sym, mid)
                elif item.get("T") == "t": # trade update
                    sym = item.get("S")
                    px  = item.get("p", 0)
                    if sym and px:
                        self.on_price(sym, px)
                elif item.get("T") == "success" and item.get("msg") == "authenticated":
                    # subscribe to quotes for all tickers
                    ws.send(json.dumps({"action":"subscribe","quotes":self.tickers,"trades":self.tickers}))
                    log.info(f"âœ… Streaming live quotes for: {', '.join(self.tickers)}")

        def on_error(ws, err):
            log.error(f"WebSocket error: {err}")

        def on_close(ws, *args):
            log.info("WebSocket closed")

        # v10.7: reconnect in a LOOP, not via recursive self._run() (no stack growth)
        while self.running:
            self.ws = websocket.WebSocketApp(
                STREAM_URL,
                on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close
            )
            self.ws.run_forever()
            if self.running:
                log.info("WebSocket closed -- reconnecting in 5s")
                time.sleep(5)

streamer = None

def on_live_price(symbol, price):
    with STATE_LOCK:
        state["prices"][symbol] = round(price, 2)


def fetch_company_news(ticker, limit=10):
    """Fetch company news from Finnhub for sentiment/display."""
    ticker = (ticker or "").upper()
    if not ticker or not FINNHUB_KEY:
        return []
    today = datetime.utcnow().date()
    start = today.replace(day=1).isoformat()
    payload = finnhub_get("/company-news", {"symbol": ticker, "from": start, "to": today.isoformat()}) or []
    news = []
    for item in payload[:limit]:
        news.append({
            "headline": item.get("headline"),
            "source": item.get("source"),
            "url": item.get("url"),
            "summary": item.get("summary"),
            "datetime": item.get("datetime"),
        })
    return news


def get_sentiment_for_ticker(ticker, strategy_signal="WATCH"):
    """Cached OpenAI + FinBERT sentiment. Safe for dashboard refreshes."""
    ticker = (ticker or "").upper()
    if not ticker:
        return {"available": False, "error": "ticker is required"}
    cache_key = f"{ticker}:{strategy_signal}"
    cached = SENTIMENT_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached.get("ts", 0) < SENTIMENT_TTL_SECONDS:
        return cached["data"]
    if analyze_ticker_sentiment is None:
        return {"available": False, "ticker": ticker, "error": "sentiment engine is not available"}
    news = fetch_company_news(ticker, limit=10)
    data = analyze_ticker_sentiment(ticker, news, strategy_signal=strategy_signal)
    data["available"] = True
    SENTIMENT_CACHE[cache_key] = {"ts": now, "data": data}
    return data


def refresh_after_order(reason="order"):
    def _refresh():
        time.sleep(2)
        previous_positions = state.get("positions", [])
        fresh_positions = fetch_positions()
        fresh_orders = fetch_orders()
        with STATE_LOCK:   # v12.1 #5: atomic multi-field update (was unlocked)
            if fresh_positions is not None:
                state["positions"] = fresh_positions
            if fresh_orders is not None:
                state["orders"] = fresh_orders
            state["reconciliation"] = reconcile_broker_state(
                state.get("positions", []), state.get("orders", []), previous_positions
            )
            state["last_updated"] = datetime.now().isoformat()
        log.info(f"Broker reconciliation after {reason}: {state['reconciliation']['status']}")
    threading.Thread(target=_refresh, daemon=True).start()

# â”€â”€ Background refresh (REST polling fallback) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def refresh_loop():
    while True:
        if state["connected"]:
            try:
                log.info("ðŸ”„ Refreshing data from Alpaca...")
                # Fetch positions FIRST to get accurate ticker list
                previous_positions = state.get("positions", [])
                positions = fetch_positions()
                account   = fetch_account()
                orders    = fetch_orders()
                reconciliation = reconcile_broker_state(positions, orders, previous_positions)

                # Get prices for current positions + core tickers
                pos_tickers = [p["ticker"] for p in positions] if positions else []
                core_tickers= ["SPY","QQQ","NVDA","MSFT","AAPL","TSLA","META","GOOGL"]
                all_tickers = list(set(pos_tickers + core_tickers))
                prices      = fetch_prices(all_tickers)

                # Update state
                with STATE_LOCK:   # v12.1 #5: atomic multi-field update (was unlocked)
                    state["prices"]         = {**DEMO_PRICES, **prices}
                    state["positions"]      = positions if positions is not None else []
                    state["account"]        = account
                    state["orders"]         = orders
                    state["reconciliation"] = reconciliation
                    state["last_updated"]   = datetime.now().isoformat()

                log.info(f"âœ… {len(state['positions'])} positions | "
                         f"${account.get('equity',0):,.0f} equity | "
                         f"{len(orders)} recent orders")
            except Exception as e:
                log.error(f"Refresh loop error: {e}")
        time.sleep(30)



def _compact_positions(positions):
    rows = []
    for p in positions or []:
        rows.append(f"{p.get('ticker') or p.get('symbol')}: qty={p.get('shares') or p.get('qty')} avg={p.get('avgCost') or p.get('avg_entry_price')} pnl={p.get('unrealizedPL') or p.get('unrealized_pl')}")
    return "\n".join(rows) if rows else "No open positions"


def ask_ai_advisor(message: str, history=None, account=None, positions=None, signals=None):
    """Server-side AI advisor call. Browser never sees the Anthropic API key."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"available": False, "error": "ANTHROPIC_API_KEY is not configured on the backend .env"}
    if not REQUESTS_OK:
        return {"available": False, "error": "requests is not installed"}
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    account = account or state.get("account", {})
    positions = positions or state.get("positions", [])
    signals = signals or []
    equity = account.get("equity", 0)
    buy_signals = [s for s in signals if str(s.get("action") or s.get("label") or "").upper() == "BUY"][:10]
    watch_signals = [s for s in signals if str(s.get("action") or s.get("label") or "").upper() == "WATCH"][:10]
    system = (
        "You are KTrade AI Advisor for a PAPER trading dashboard. "
        "Use only the account, position, signal, and risk context provided. "
        "Be specific and educational. Do not claim certainty or give live-money financial advice.\n\n"
        f"Account equity: {equity}\n"
        f"Open positions:\n{_compact_positions(positions)}\n\n"
        f"BUY signals:\n{json.dumps(buy_signals, default=str)[:4000]}\n\n"
        f"WATCH signals:\n{json.dumps(watch_signals, default=str)[:4000]}"
    )
    messages = []
    for m in (history or [])[-8:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("text") or m.get("content") or "")[:2000]
        if text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": str(message)[:4000]})
    try:
        r = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={"model": model, "max_tokens": 1000, "system": system, "messages": messages},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        text = (data.get("content") or [{}])[0].get("text", "No response")
        return {"available": True, "answer": text, "model": model}
    except Exception as exc:
        log.error("AI advisor failed: %s", exc)
        return {"available": False, "error": str(exc)}

# â”€â”€ Flask API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app = Flask(__name__)
CORS(app, origins=os.getenv("KTRADE_ALLOWED_ORIGINS",
    "http://localhost:5001,http://127.0.0.1:5001").split(","))

# v10.6: admin token gate for order endpoints (/buy /sell /cancel).
ADMIN_TOKEN = os.getenv("KTRADE_ADMIN_TOKEN", "").strip()
def require_admin():
    """Return an error response tuple if the admin token is missing/invalid, else None."""
    if not ADMIN_TOKEN:
        return jsonify({"error": "KTRADE_ADMIN_TOKEN not configured; order endpoints disabled"}), 403
    if request.headers.get("X-KTrade-Admin-Token", "") != ADMIN_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None

def _manual_allow_demo() -> bool:
    return os.getenv("KTRADE_MANUAL_ALLOW_DEMO", "false").strip().lower() in {"1", "true", "yes", "on"}


_MANUAL_RISK_LOCK = _RLock()
_MANUAL_RISK_ENGINE = None
_MANUAL_RISK_EQUITY_INITIALIZED = False
_MANUAL_RISK_DAY = None


def _parse_scan_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _scan_payload_is_fresh(payload):
    """v12.6: reject scan files older than KTRADE_MAX_SCAN_AGE_MINUTES (default 30)
    so trading paths never act on stale BUY signals or stale price references."""
    max_age = int(os.getenv("KTRADE_MAX_SCAN_AGE_MINUTES", "30"))
    generated_at = _parse_scan_time((payload or {}).get("generated_at", ""))
    if generated_at is None:
        return False, "scan file missing/invalid generated_at"
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - generated_at).total_seconds() / 60.0
    if age_min > max_age:
        return False, f"scan file stale: {age_min:.1f} min old (max {max_age})"
    return True, "fresh"


def _load_valid_scan_payload_safe() -> dict:
    """Load scanner JSON through the schema validator. Never raises."""
    try:
        from data.scan_schema import load_valid_scan_payload
        return load_valid_scan_payload(PROJECT_DIR / "data" / "ktrade_scan_latest.json")
    except Exception as exc:
        log.warning("could not load validated scan payload: %s", exc)
        return {"results": [], "errors": [str(exc)]}


def _trusted_reference_price(ticker: str) -> float | None:
    """Return a trusted non-snapshot reference price for manual BUY validation.

    Manual orders must not seed the bad-tick guard from the current Alpaca
    snapshot because a bad current tick would validate itself. Prefer the
    scanner's price_validation.reference_price, which is created from the
    scanner/reference pipeline. If absent, block first BUY and ask the user to
    run a scan first.
    """
    ticker = str(ticker or "").upper()
    payload = _load_valid_scan_payload_safe()
    fresh, _why = _scan_payload_is_fresh(payload)
    if not fresh:
        log.warning("manual BUY reference rejected: %s", _why)
        return None
    for row in payload.get("results", []) or []:
        if str(row.get("ticker") or "").upper() != ticker:
            continue
        pv = row.get("price_validation") or {}
        for key in ("reference_price", "reference", "prior_close"):
            try:
                ref = float(pv.get(key) or 0)
            except Exception:
                ref = 0.0
            if ref > 0:
                return ref
    return None


def _get_manual_risk_engine():
    """Return the process-wide manual-order RiskEngine.

    v12 cleanup: dashboard/manual execution uses one shared, persistent
    RiskEngine instead of a new engine per request. This preserves duplicate
    windows, ticker cooldowns, daily counters, kill state, and state-store data.
    """
    global _MANUAL_RISK_ENGINE
    if _MANUAL_RISK_ENGINE is not None:
        return _MANUAL_RISK_ENGINE
    from agent.ktrade_agent_v9 import RiskEngine
    eng = RiskEngine()
    try:
        from risk.state_store import RiskStateStore
        eng.state_store = RiskStateStore()
        eng.restore_state(eng.state_store.load())
    except Exception as exc:
        log.warning("manual RiskEngine state-store unavailable: %s", exc)
    try:
        from data.earnings_calendar import EarningsCalendar
        eng.earnings_cal = EarningsCalendar()
    except Exception:
        pass
    _MANUAL_RISK_ENGINE = eng
    return eng


def _manual_sync_risk_engine(eng, equity: float) -> None:
    """Sync broker truth and roll daily counters for dashboard/manual orders."""
    global _MANUAL_RISK_EQUITY_INITIALIZED, _MANUAL_RISK_DAY
    today = datetime.now().date()
    if not _MANUAL_RISK_EQUITY_INITIALIZED:
        eng.equity_open = equity
        eng.equity = equity
        _MANUAL_RISK_EQUITY_INITIALIZED = True
        _MANUAL_RISK_DAY = today
    elif _MANUAL_RISK_DAY != today:
        eng.reset_day()
        eng.equity_open = equity
        _MANUAL_RISK_DAY = today
    eng.update_equity(equity)
    eng.sync_positions(fetch_positions())


def _risk_gate_manual_decision(parsed: dict) -> dict:
    """Full dashboard/manual order risk gate.

    Returns a dict with approved/reason/qty/price/stop/target. Any exception
    blocks. Manual BUY requires a trusted reference price from the scanner; it
    does not self-seed from the current broker snapshot.
    """
    try:
        from agent.ktrade_agent_v9 import TradeRequest, CFG
        ticker = str(parsed["ticker"]).upper()
        side = str(parsed["side"]).lower()
        requested_qty = float(parsed["qty"])
        if requested_qty <= 0:
            return {"approved": False, "reason": "qty must be > 0", "qty": 0.0}

        snap = fetch_prices([ticker]) or {}
        px = float(parsed.get("limit_price") or snap.get(ticker) or state.get("prices", {}).get(ticker) or 0)
        if px <= 0:
            return {"approved": False, "reason": f"no valid price for {ticker}", "qty": 0.0}

        acct = fetch_account() or state.get("account") or DEMO_ACCOUNT
        equity = float(acct.get("equity") or acct.get("portfolio_value") or DEMO_ACCOUNT["equity"])

        with _MANUAL_RISK_LOCK:
            eng = _get_manual_risk_engine()
            _manual_sync_risk_engine(eng, equity)

            if side == "buy":
                ref = _trusted_reference_price(ticker)
                if ref is None:
                    return {
                        "approved": False,
                        "reason": f"no trusted price reference for {ticker}; run scanner before manual BUY",
                        "qty": 0.0,
                        "price": px,
                    }
                eng.seed_references({ticker: ref})
            else:
                # Do not accidentally short from the dashboard unless explicitly allowed.
                pos = eng.open_positions.get(ticker) or {}
                open_qty = float(pos.get("qty", 0) or 0)
                if open_qty > 0:
                    requested_qty = min(requested_qty, open_qty)
                elif not CFG.allow_shorts:
                    return {"approved": False, "reason": "no open long position; shorts disabled", "qty": 0.0}

            trade = TradeRequest(
                ticker=ticker,
                side=side,
                qty=requested_qty,
                price=px,
                conviction=100,
                desired_risk_dollars=equity * CFG.risk_per_trade,
            )
            decision = eng.evaluate(trade)
            eng.persist_state()

        if not decision.approved:
            return {"approved": False, "reason": decision.reason, "qty": 0.0, "price": px}

        sized = min(requested_qty, float(decision.approved_qty)) if side == "buy" else requested_qty
        if sized <= 0:
            return {"approved": False, "reason": "risk-sized to 0", "qty": 0.0, "price": px}
        return {
            "approved": True,
            "reason": "approved",
            "qty": sized,
            "price": px,
            "stop": float(decision.stop_price or 0),
            "target": float(decision.target_price or 0),
            "decision": decision,
        }
    except Exception as exc:
        log.exception("manual risk gate unavailable -> blocking")
        return {"approved": False, "reason": f"risk engine unavailable: {exc}", "qty": 0.0}


def _risk_gate_manual(parsed: dict):
    """Backward-compatible wrapper used by the existing auto-trade path."""
    d = _risk_gate_manual_decision(parsed)
    return bool(d.get("approved")), str(d.get("reason") or "blocked"), float(d.get("qty") or 0.0)

def _load_current_signals():
    """Load the latest scanner signals used by auto-trading. v12.6: reject stale
    scan files so the agent never trades on outdated signals."""
    try:
        scan_path = PROJECT_DIR / "data" / "ktrade_scan_latest.json"
        if not scan_path.exists():
            return []
        import sys as _sys, os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if root not in _sys.path:
            _sys.path.insert(0, root)
        try:
            from data.scan_schema import load_valid_scan_payload
            payload = load_valid_scan_payload(scan_path)
        except Exception:
            payload = json.loads(scan_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            payload = {"results": payload}
        fresh, why = _scan_payload_is_fresh(payload)
        if not fresh:
            log.warning("auto-trade scan rejected: %s", why)
            return []
        return payload.get("results", []) or []
    except Exception as exc:
        log.warning("auto-trade signal load failed: %s", exc)
        return []


def _auto_trade_status_snapshot():
    with AUTO_TRADE_LOCK:
        payload = dict(AUTO_TRADE_STATE)
        payload.update({
            "order_submission_enabled": ORDER_SUBMISSION_ENABLED,
            "interval_seconds": AUTO_TRADE_INTERVAL_SECONDS,
            "min_conviction": AUTO_TRADE_MIN_CONVICTION,
            "qty": AUTO_TRADE_QTY,
            "max_orders_per_cycle": AUTO_TRADE_MAX_ORDERS_PER_CYCLE,
            "stop_loss_pct": AUTO_TRADE_STOP_LOSS_PCT,
            "take_profit_pct": AUTO_TRADE_TAKE_PROFIT_PCT,
            "require_sentiment": AUTO_TRADE_REQUIRE_SENTIMENT,
            "connected": state.get("connected"),
            "backend_mode": state.get("mode"),
        })
        return payload


def _signal_ticker(signal):
    return str(signal.get("ticker") or signal.get("symbol") or "").strip().upper()


def _signal_action(signal):
    return str(signal.get("action") or signal.get("label") or signal.get("signal") or "WATCH").strip().upper()


def _signal_conviction(signal):
    try:
        return float(signal.get("conviction") or signal.get("score") or 0)
    except Exception:
        return 0.0


def _open_position_symbols():
    return {str(p.get("ticker") or p.get("symbol") or "").upper() for p in (fetch_positions() or state.get("positions", [])) if (p.get("ticker") or p.get("symbol"))}


def _pending_order_symbols():
    pending = {"new", "accepted", "pending_new", "partially_filled", "held", "pending_cancel"}
    symbols = set()
    for order in fetch_orders() or state.get("orders", []):
        status = str(order.get("status") or "").lower()
        if status in pending:
            sym = str(order.get("ticker") or order.get("symbol") or "").upper()
            if sym:
                symbols.add(sym)
    return symbols


def place_bracket_order(ticker, qty, side, stop_price, target_price, client_order_id=None, order_type="market", limit_price=None):
    """Submit an Alpaca bracket order with broker-side stop-loss and take-profit.

    v12: manual dashboard BUY also uses this helper so every entry has a
    broker-side stop and target. Parent order can be market or limit.
    """
    # v11.1: in LIVE mode, an un-graduated symbol may not open new exposure.
    # No-op in paper mode and for exits (sells are never gated).
    # v12.6: defensive validation — never submit an order with bad qty/side/bracket.
    ticker = str(ticker or "").strip().upper()
    side = str(side or "").lower()
    if not ticker:
        raise ValueError("ticker is required")
    if side not in {"buy", "sell"}:
        raise ValueError(f"invalid side: {side}")
    qty_i = int(float(qty or 0))
    if qty_i < 1:
        raise ValueError(f"qty must be at least 1 whole share; got {qty}")
    stop = float(stop_price or 0)
    target = float(target_price or 0)
    if stop <= 0 or target <= 0:
        raise ValueError("stop_price and target_price must be positive")
    if side == "buy" and stop >= target:
        raise ValueError(f"invalid BUY bracket: stop {stop} must be below target {target}")
    if str(side).lower() == "buy":
        _blk = _promo_block(ticker)
        if _blk:
            log.info("[PROMOTION] live bracket BUY blocked for %s — %s", ticker.upper(), _blk)
            return None
    body = {
        "symbol": ticker.upper(),
        "qty": str(qty_i),
        "side": side,
        "type": str(order_type or "market").lower(),
        "time_in_force": "day",
        "client_order_id": client_order_id or f"ktrade-auto-{ticker.upper()}-{uuid.uuid4().hex[:12]}",
        "order_class": "bracket",
        "stop_loss": {"stop_price": str(round(stop, 2))},
        "take_profit": {"limit_price": str(round(target, 2))},
    }
    if str(order_type or "market").lower() == "limit":
        if limit_price is None or float(limit_price) <= 0:
            raise ValueError("limit_price is required for limit bracket orders")
        body["limit_price"] = str(round(float(limit_price), 2))
    return alpaca_post("/v2/orders", body)


def _auto_trade_once():
    """One auto-trading pass: BUY approved BUY signals using bracket orders."""
    with AUTO_TRADE_LOCK:
        AUTO_TRADE_STATE["cycles"] += 1
        AUTO_TRADE_STATE["last_cycle_at"] = datetime.now().isoformat()
        AUTO_TRADE_STATE["last_error"] = None
        AUTO_TRADE_STATE["blocked"] = []

    if not ORDER_SUBMISSION_ENABLED:
        raise RuntimeError("KTRADE_PAPER_ORDER_SUBMISSION is false; auto trading is blocked")
    if not state.get("connected") and not AUTO_TRADE_ALLOW_DEMO:
        raise RuntimeError("backend is not connected to Alpaca paper account")
    if DESYNC_STATE.get("halt"):
        raise RuntimeError("broker reconciliation halt is active")

    # v11.3: portfolio-level kill switch (net-worth drawdown + feed staleness).
    _pf_allow, _pf_reason = _portfolio_cycle_gate()
    if not _pf_allow:
        _audit("cycle_halt", {"reason": _pf_reason})
        with AUTO_TRADE_LOCK:
            AUTO_TRADE_STATE["last_action"] = f"HALT (portfolio): {_pf_reason}"
        log.critical("[PORTFOLIO] auto-trade cycle halted -- %s", _pf_reason)
        return []

    signals = _load_current_signals()
    if not signals:
        with AUTO_TRADE_LOCK:
            AUTO_TRADE_STATE["last_action"] = "No scanner signals found. Run scanner first."
        return []

    open_symbols = _open_position_symbols()
    pending_symbols = _pending_order_symbols()
    submitted = []
    blocked = []

    # v11.2: pick this cycle's strategy profile from the live regime + VIX.
    # Baseline (no behaviour change) unless KTRADE_STRATEGY_SWITCH_ENABLED.
    _profile = _active_strategy_profile(AUTO_TRADE_MIN_CONVICTION)
    _min_conv = _profile.min_conviction
    _deployed = _current_deployed_notional()  # v11.3: trading sub-account exposure

    for sig in signals:
        if len(submitted) >= AUTO_TRADE_MAX_ORDERS_PER_CYCLE:
            break
        ticker = _signal_ticker(sig)
        action = _signal_action(sig)
        conviction = _signal_conviction(sig)
        if not ticker or action != "BUY":
            continue
        if not _profile.allow_new_longs:
            blocked.append({"ticker": ticker,
                            "reason": f"{_profile.regime} regime ({_profile.name}): new longs blocked"})
            continue
        if conviction < _min_conv:
            blocked.append({"ticker": ticker,
                            "reason": f"conviction {conviction} < {_min_conv:.0f} ({_profile.name})"})
            continue
        if ticker in open_symbols:
            blocked.append({"ticker": ticker, "reason": "already has open position"})
            continue
        if ticker in pending_symbols:
            blocked.append({"ticker": ticker, "reason": "already has pending order"})
            continue
        if AUTO_TRADE_REQUIRE_SENTIMENT:
            sentiment = get_sentiment_for_ticker(ticker, strategy_signal="BUY")
            if str(sentiment.get("sentiment") or "").lower() not in {"positive", "bullish"}:
                blocked.append({"ticker": ticker, "reason": "sentiment confirmation failed"})
                continue

        qty = AUTO_TRADE_QTY
        parsed = {"ticker": ticker, "side": "buy", "qty": qty, "order_type": "market", "type": "market"}
        approved, reason, sized_qty = _risk_gate_manual(parsed)
        if not approved:
            blocked.append({"ticker": ticker, "reason": f"RiskEngine blocked: {reason}"})
            continue
        raw_sized_qty = float(sized_qty or 0)
        qty = int(raw_sized_qty)   # v12.6: floor only — never round up past approved size
        if qty < 1:
            blocked.append({"ticker": ticker, "reason": (
                f"RiskEngine approved < 1 whole share ({raw_sized_qty:.4f}); refusing to round up")})
            continue
        # v11.2: scale by the regime size multiplier (0.5 in RISK_OFF, 1.0 otherwise).
        if _profile.size_mult != 1.0:
            adjusted_qty = int(qty * _profile.size_mult)
            if adjusted_qty < 1:
                blocked.append({"ticker": ticker, "reason": (
                    f"Regime size multiplier reduced qty below 1 "
                    f"({qty} * {_profile.size_mult}); refusing to round up")})
                continue
            qty = adjusted_qty
        px = float((fetch_prices([ticker]) or {}).get(ticker) or state.get("prices", {}).get(ticker) or 0)
        if px <= 0:
            blocked.append({"ticker": ticker, "reason": "no valid price"})
            continue
        stop = px * (1 - AUTO_TRADE_STOP_LOSS_PCT / 100.0)
        target = px * (1 + AUTO_TRADE_TAKE_PROFIT_PCT / 100.0)
        # v11.3: portfolio-level exposure cap (fraction of TOTAL net worth).
        _notional = qty * px
        _exp_ok, _exp_why = _portfolio_exposure_ok(_notional, _deployed)
        if not _exp_ok:
            blocked.append({"ticker": ticker, "reason": f"portfolio cap: {_exp_why}"})
            _audit("blocked", {"ticker": ticker, "stage": "portfolio_exposure",
                               "reason": _exp_why, "notional": round(_notional, 2)})
            continue
        order = place_bracket_order(ticker, qty, "buy", stop, target)
        if not order:
            blocked.append({"ticker": ticker, "reason": "broker order failed"})
            continue
        submitted.append({
            "ticker": ticker,
            "qty": qty,
            "entry_ref": round(px, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "order_id": order.get("id"),
            "client_order_id": order.get("client_order_id"),
        })
        open_symbols.add(ticker)
        pending_symbols.add(ticker)
        _deployed += qty * px  # v11.3: track running deployment within the cycle
        _audit("submitted", {
            "ticker": ticker, "qty": qty, "entry_ref": round(px, 2),
            "stop": round(stop, 2), "target": round(target, 2),
            "profile": getattr(_profile, "name", "?"), "regime": getattr(_profile, "regime", "?"),
            "order_id": order.get("id"), "client_order_id": order.get("client_order_id"),
        })

    if submitted:
        refresh_after_order("AUTO")
    with AUTO_TRADE_LOCK:
        AUTO_TRADE_STATE["orders_submitted"] += len(submitted)
        AUTO_TRADE_STATE["blocked"] = blocked[-20:]
        AUTO_TRADE_STATE["last_action"] = f"submitted {len(submitted)} order(s), blocked {len(blocked)}"
    return submitted


def _auto_trade_worker():
    log.info("Auto trading worker started")
    with AUTO_TRADE_LOCK:
        AUTO_TRADE_STATE["running"] = True
    try:
        while True:
            with AUTO_TRADE_LOCK:
                enabled = AUTO_TRADE_STATE["enabled"]
            if not enabled:
                break
            try:
                submitted = _auto_trade_once()
                log.info("Auto trading cycle complete: %d submitted", len(submitted))
            except Exception as exc:
                log.error("Auto trading cycle blocked/failed: %s", exc)
                with AUTO_TRADE_LOCK:
                    AUTO_TRADE_STATE["last_error"] = str(exc)
                    AUTO_TRADE_STATE["last_action"] = "blocked/error"
            # Sleep in short chunks so Stop reacts quickly.
            for _ in range(max(1, AUTO_TRADE_INTERVAL_SECONDS)):
                with AUTO_TRADE_LOCK:
                    if not AUTO_TRADE_STATE["enabled"]:
                        break
                time.sleep(1)
    finally:
        with AUTO_TRADE_LOCK:
            AUTO_TRADE_STATE["running"] = False
            AUTO_TRADE_STATE["enabled"] = False
            AUTO_TRADE_STATE["stopped_at"] = datetime.now().isoformat()
        log.info("Auto trading worker stopped")

@app.route("/")
def index():
    return send_from_directory(PROJECT_DIR / "frontend", "KTrade_preview.html")


@app.route("/ai/advisor", methods=["POST"])
def ai_advisor():
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"available": False, "error": "message is required"}), 400
    result = ask_ai_advisor(
        message,
        history=body.get("history") or [],
        account=body.get("account") or state.get("account", {}),
        positions=body.get("positions") or state.get("positions", []),
        signals=body.get("signals") or [],
    )
    status = 200 if result.get("available") else 503
    return jsonify(result), status

@app.route("/copilot/report")
def copilot_report():
    """v13.1: decision-level analysis of the copilot shadow ledger (how often it
    fired, agreed, disagreed, abstained, and which names it vetoed). Outcome
    scoring (forward returns) is available via agent/copilot_analysis.py with a
    price source; this endpoint returns the no-price decision stats for the UI."""
    try:
        from agent.copilot_analysis import load_ledger, build_report
        path = os.getenv("KTRADE_COPILOT_LEDGER",
                         str(PROJECT_DIR / "logs" / "ktrade_copilot_ledger.jsonl"))
        try:
            horizon = int(request.args.get("horizon", 5))
        except (TypeError, ValueError):
            horizon = 5
        report = build_report(load_ledger(path), price_at=None, horizon_days=horizon)
        return jsonify({"ok": True, "ledger": path, "report": report})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/health")
def health():
    snap = snapshot_state()
    return jsonify({
        "ok": True,
        "connected": snap.get("connected"),
        "mode": snap.get("mode"),
        "paper_orders_enabled": ORDER_SUBMISSION_ENABLED,
        "positions": len(snap.get("positions", [])),
        "last_updated": snap.get("last_updated"),
        "reconciliation": snap.get("reconciliation", {}),
        "errors": snap.get("errors", [])[-5:],
        "market_data_sources": {
            "alpaca": bool(API_KEY and API_SECRET),
            "finnhub": bool(FINNHUB_KEY),
        },
    })


@app.route("/runtime/status")
def runtime_status_route():
    if runtime_status is None:
        return jsonify({"available": False, "error": "runtime helpers unavailable"}), 503
    return jsonify({"available": True, **runtime_status()})


@app.route("/auto/status")
def auto_status():
    return jsonify(_auto_trade_status_snapshot())


@app.route("/auto/start", methods=["POST"])
def auto_start():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    if not ORDER_SUBMISSION_ENABLED:
        return jsonify({"ok": False, "error": "Set KTRADE_PAPER_ORDER_SUBMISSION=true in .env first."}), 403
    if not state.get("connected") and not AUTO_TRADE_ALLOW_DEMO:
        return jsonify({"ok": False, "error": "Backend is not connected to Alpaca paper account."}), 403
    with AUTO_TRADE_LOCK:
        if AUTO_TRADE_STATE["running"] or AUTO_TRADE_STATE["enabled"]:
            return jsonify({"ok": True, "status": _auto_trade_status_snapshot(), "message": "Auto trading already running"})
        AUTO_TRADE_STATE["enabled"] = True
        AUTO_TRADE_STATE["started_at"] = datetime.now().isoformat()
        AUTO_TRADE_STATE["stopped_at"] = None
        AUTO_TRADE_STATE["last_error"] = None
        AUTO_TRADE_STATE["last_action"] = "starting"
    t = threading.Thread(target=_auto_trade_worker, daemon=True)
    t.start()
    return jsonify({"ok": True, "status": _auto_trade_status_snapshot()})


@app.route("/auto/stop", methods=["POST"])
def auto_stop():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    with AUTO_TRADE_LOCK:
        AUTO_TRADE_STATE["enabled"] = False
        AUTO_TRADE_STATE["last_action"] = "stop requested"
        AUTO_TRADE_STATE["stopped_at"] = datetime.now().isoformat()
    return jsonify({"ok": True, "status": _auto_trade_status_snapshot()})


@app.route("/emergency/kill", methods=["POST"])
def emergency_kill():
    """v12.4: real kill switch. Triggers the persistent EmergencyController
    (records kill state, cancels open orders, optionally flattens positions via
    the broker adapter) and disables the auto worker. Distinct from /auto/stop,
    which only pauses the worker."""
    auth_error = require_admin()
    if auth_error:
        return auth_error
    body = request.get_json(silent=True) or {}
    flatten = bool(body.get("flatten", False))
    try:
        from agent.broker_adapter import AlpacaBrokerAdapter
        from risk.emergency import EmergencyController
        broker = AlpacaBrokerAdapter()
        EmergencyController(broker=broker).trigger(
            "manual dashboard emergency kill", flatten=flatten
        )
        with AUTO_TRADE_LOCK:
            AUTO_TRADE_STATE["enabled"] = False
            AUTO_TRADE_STATE["last_action"] = f"EMERGENCY KILL (flatten={flatten})"
            AUTO_TRADE_STATE["stopped_at"] = datetime.now().isoformat()
        try:
            _audit("emergency_kill", {"flatten": flatten})
        except Exception:
            pass
        return jsonify({"ok": True, "flatten": flatten})
    except Exception as exc:
        log.exception("Emergency kill failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/auto/run-once", methods=["POST"])
def auto_run_once():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    try:
        submitted = _auto_trade_once()
        return jsonify({"ok": True, "submitted": submitted, "status": _auto_trade_status_snapshot()})
    except Exception as exc:
        with AUTO_TRADE_LOCK:
            AUTO_TRADE_STATE["last_error"] = str(exc)
        return jsonify({"ok": False, "error": str(exc), "status": _auto_trade_status_snapshot()}), 400


@app.route("/all")
def all_data():
    """Single call â€” KTrade frontend calls this for everything"""
    # Load + VALIDATE signals from ktrade_scan_latest.json (v10.7 #6)
    signals = []
    try:
        scan_path = PROJECT_DIR / "data" / "ktrade_scan_latest.json"
        if scan_path.exists():
            try:
                import sys as _sys, os as _os
                root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                if root not in _sys.path:
                    _sys.path.insert(0, root)
                from data.scan_schema import load_valid_scan_payload
                signals = load_valid_scan_payload(scan_path).get("results", [])
            except Exception:
                data = json.loads(scan_path.read_text(encoding="utf-8"))
                signals = data.get("results", data if isinstance(data, list) else [])
    except Exception as e:
        log.warning(f"Could not load signals: {e}")

    snap = snapshot_state()   # v10.7 #10: consistent locked read
    return jsonify({
        "connected":    snap.get("connected"),
        "mode":         snap.get("mode"),
        "account":      snap.get("account"),
        "positions":    snap.get("positions"),
        "prices":       snap.get("prices"),
        "orders":       snap.get("orders"),
        "signals":      signals,
        "reconciliation": snap.get("reconciliation", {}),
        "last_updated": snap.get("last_updated"),
        "auto_trading": _auto_trade_status_snapshot()
    })

@app.route("/reconciliation")
def reconciliation_status():
    return jsonify(snapshot_state().get("reconciliation", {}))

@app.route("/closed_trades")
def closed_trades():
    """v10.1: verified closed trades from real fills (trustworthy P&L)."""
    try:
        return jsonify(build_truth_closed_trades())
    except Exception as exc:
        return jsonify({"trades": [], "stats": None, "error": str(exc)}), 500

@app.route("/reconcile_truth")
def reconcile_truth():
    """v10.1: desync/halt signal the agent can poll to trip its kill switch."""
    dashboard = state.get("positions", [])
    intended = {(p.get("ticker") or "").upper(): float(p.get("shares") or 0)
                for p in dashboard if p.get("ticker")}
    payload = build_truth_closed_trades(intended_book=intended)
    return jsonify({"halt": payload["halt"], "desyncs": payload["desyncs"],
                    "checked_at": DESYNC_STATE["checked_at"]})

@app.route("/agent/status")
def agent_status():
    scan_path = PROJECT_DIR / "data" / "ktrade_scan_latest.json"
    if not scan_path.exists():
        return jsonify({
            "available": False,
            "message": "Run run-score-only.ps1 to generate a KTrade v9 scan."
        })
    try:
        return jsonify(json.loads(scan_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        return jsonify({"available": False, "error": str(exc)}), 500


@app.route("/portfolio/status")
def portfolio_status():
    """Portfolio source-of-truth status for dashboard/health checks."""
    if _get_portfolio is None:
        return jsonify({"enabled": _portfolio_gate_enabled_env(), "available": False, "error": "portfolio module absent"})
    try:
        status = _get_portfolio().status()
        ok, reason = _portfolio_cycle_gate()
        status.update({"cycle_allowed": bool(ok), "cycle_reason": reason})
        return jsonify(status)
    except Exception as exc:
        return jsonify({"enabled": _portfolio_gate_enabled_env(), "available": False, "error": str(exc)}), 500


@app.route("/intraday-backtest/latest")
def intraday_backtest_latest():
    report_path = PROJECT_DIR / "data" / "ktrade_intraday_backtest_latest.json"
    if not report_path.exists():
        return jsonify({"available": False, "message": "Run ktrade_intraday_vectorbt.py to generate an intraday backtest report."})
    try:
        return jsonify(json.loads(report_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        return jsonify({"available": False, "error": str(exc)}), 500

@app.route("/intraday-approved-params")
def intraday_approved_params():
    params_path = PROJECT_DIR / "data" / "ktrade_intraday_approved_params.json"
    if not params_path.exists():
        return jsonify({"available": False, "message": "No intraday approved parameters yet."})
    try:
        return jsonify(json.loads(params_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        return jsonify({"available": False, "error": str(exc)}), 500
@app.route("/account")
def account():
    return jsonify(snapshot_state().get("account", {}))

@app.route("/positions")
def positions():
    return jsonify(snapshot_state().get("positions", []))

@app.route("/prices")
def prices():
    return jsonify(snapshot_state().get("prices", {}))

@app.route("/orders")
def orders():
    return jsonify(snapshot_state().get("orders", []))

@app.route("/quote/<ticker>")
def quote(ticker):
    ticker = ticker.upper()
    if state["connected"]:
        data = fetch_prices([ticker])
        price = data.get(ticker, state["prices"].get(ticker, 0))
    else:
        price = state["prices"].get(ticker, 0)
    source = "finnhub" if FINNHUB_KEY and ticker not in state["prices"] else "alpaca"
    return jsonify({"ticker": ticker, "price": price, "mode": state["mode"], "source": source})

@app.route("/news/<ticker>")
def ticker_news(ticker):
    """Optional Finnhub company news for dashboard/sentiment display."""
    ticker = ticker.upper()
    if not FINNHUB_KEY:
        return jsonify({"ticker": ticker, "available": False, "news": [], "message": "FINNHUB_API_KEY is not configured"})
    return jsonify({"ticker": ticker, "available": True, "news": fetch_company_news(ticker), "source": "finnhub"})

@app.route("/sentiment/<ticker>")
def ticker_sentiment(ticker):
    """OpenAI + FinBERT sentiment confirmation for a ticker."""
    signal = request.args.get("signal", "WATCH")
    return jsonify(get_sentiment_for_ticker(ticker, strategy_signal=signal))

@app.route("/buy", methods=["POST"])
def buy():
    """
    POST /buy
    { "ticker": "NVDA", "qty": 1, "type": "market" }
    or
    { "ticker": "NVDA", "qty": 1, "type": "limit", "limit_price": 130.00 }

    v12 cleanup: manual BUY now uses strict schema -> shared RiskEngine ->
    Alpaca bracket order. It refuses to trade from demo/offline mode unless
    KTRADE_MANUAL_ALLOW_DEMO=true.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "No body"}), 400
    if not ORDER_SUBMISSION_ENABLED:
        return jsonify({"error": "Paper order submission is disabled"}), 403
    auth_error = require_admin()
    if auth_error:
        return auth_error
    if not state.get("connected") and not _manual_allow_demo():
        return jsonify({"error": "Backend is not connected to Alpaca paper; refusing manual BUY"}), 503

    from manual_order_schema import parse_manual_order
    body = dict(body)
    body["side"] = "buy"
    parsed, perr = parse_manual_order(body)
    if perr:
        return jsonify({"error": f"invalid order: {perr}"}), 400

    gate = _risk_gate_manual_decision(parsed)
    if not gate.get("approved"):
        return jsonify({"error": f"RiskEngine blocked BUY: {gate.get('reason')}"}), 403

    ticker = parsed["ticker"]
    raw_qty = float(gate.get("qty") or 0)
    qty = int(raw_qty)   # v12.5: floor only — never round up past the risk-approved size
    if qty < 1:
        return jsonify({"error": (
            f"RiskEngine approved less than 1 whole share ({raw_qty:.4f}); "
            f"refusing to round up and increase risk."
        )}), 403
    order_type = parsed.get("order_type", "market")
    limit_price = parsed.get("limit_price")
    stop = float(gate.get("stop") or 0)
    target = float(gate.get("target") or 0)
    if stop <= 0 or target <= 0:
        px = float(gate.get("price") or 0)
        if px <= 0:
            return jsonify({"error": "No valid price for bracket stop/target"}), 400
        stop = px * (1 - AUTO_TRADE_STOP_LOSS_PCT / 100.0)
        target = px * (1 + AUTO_TRADE_TAKE_PROFIT_PCT / 100.0)

    if state.get("connected"):
        try:
            result = place_bracket_order(
                ticker, qty, "buy", stop, target,
                order_type=order_type,
                limit_price=limit_price,
            )
        except Exception as exc:
            log.error("manual bracket BUY failed: %s", exc)
            return jsonify({"error": f"Order failed: {exc}"}), 500
        if result:
            log.info("MANUAL BUY bracket %sx %s stop=%.2f target=%.2f order=%s",
                     qty, ticker, stop, target, str(result.get("id", ""))[:8])
            refresh_after_order("BUY")
            return jsonify({"ok": True, "order": result, "mode": "paper", "risk": {"stop": round(stop, 2), "target": round(target, 2)}})
        return jsonify({"error": "Order failed"}), 500

    fake = {"id": f"demo_{int(time.time())}", "symbol": ticker, "qty": str(qty),
            "side": "buy", "type": "bracket", "status": "filled",
            "filled_qty": str(qty), "mode": "demo", "stop": round(stop, 2), "target": round(target, 2)}
    log.info("[DEMO] MANUAL BUY bracket %sx %s", qty, ticker)
    return jsonify({"ok": True, "order": fake, "mode": "demo"})

@app.route("/sell", methods=["POST"])
def sell():
    """
    POST /sell
    { "ticker": "NVDA", "qty": 1, "type": "market" }

    v12 cleanup: manual SELL uses strict schema and the shared RiskEngine. It
    refuses accidental shorts unless CFG.allow_shorts is enabled.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "No body"}), 400
    if not ORDER_SUBMISSION_ENABLED:
        return jsonify({"error": "Paper order submission is disabled"}), 403
    auth_error = require_admin()
    if auth_error:
        return auth_error
    if not state.get("connected") and not _manual_allow_demo():
        return jsonify({"error": "Backend is not connected to Alpaca paper; refusing manual SELL"}), 503

    from manual_order_schema import parse_manual_order
    body = dict(body)
    body["side"] = "sell"
    parsed, perr = parse_manual_order(body)
    if perr:
        return jsonify({"error": f"invalid order: {perr}"}), 400

    gate = _risk_gate_manual_decision(parsed)
    if not gate.get("approved"):
        return jsonify({"error": f"RiskEngine blocked SELL: {gate.get('reason')}"}), 403

    ticker = parsed["ticker"]
    qty = float(gate["qty"])
    order_type = parsed.get("order_type", "market")
    limit_price = parsed.get("limit_price")

    if state.get("connected"):
        result = place_order(ticker, qty, "sell", order_type, limit_price)
        if result:
            log.info("MANUAL SELL %sx %s @ %s order=%s", qty, ticker, order_type, str(result.get("id", ""))[:8])
            refresh_after_order("SELL")
            return jsonify({"ok": True, "order": result, "mode": "paper"})
        return jsonify({"error": "Order failed"}), 500

    fake = {"id": f"demo_{int(time.time())}", "symbol": ticker, "qty": str(qty),
            "side": "sell", "type": order_type, "status": "filled",
            "filled_qty": str(qty), "mode": "demo"}
    log.info("[DEMO] MANUAL SELL %sx %s", qty, ticker)
    return jsonify({"ok": True, "order": fake, "mode": "demo"})

@app.route("/cancel/<order_id>", methods=["DELETE"])
def cancel(order_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    if state["connected"]:
        ok = cancel_order(order_id)
        return jsonify({"ok": bool(ok)})
    return jsonify({"ok":True,"mode":"demo"})

# â”€â”€ Startup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def startup():
    global streamer

    log.info("="*52)
    log.info("  KTrade PRO â€” Alpaca Paper Trading Bridge")
    log.info("="*52)

    if not API_KEY or not API_SECRET:
        log.warning("âš   No API credentials found")
        log.warning("   Set ALPACA_KEY and ALPACA_SECRET env vars")
        log.warning("   Get free keys at https://alpaca.markets")
        log.warning("   Running in DEMO mode\n")
        state["mode"] = "demo"
        state["connected"] = False
    else:
        # Test connection
        log.info(f"ðŸ” Connecting to Alpaca paper trading...")
        acct = fetch_account()
        if acct and acct.get("status") == "ACTIVE":
            state["connected"] = True
            state["mode"]      = "paper"
            state["account"]   = acct
            log.info(f"âœ… Connected! Equity: ${acct['equity']:,.2f} | Buying power: ${acct['buying_power']:,.2f}")

            # Load initial data
            positions = fetch_positions()
            tickers = [p["ticker"] for p in positions]
            new_prices = {**DEMO_PRICES, **fetch_prices(tickers)} if tickers else None
            new_orders = fetch_orders()
            with STATE_LOCK:   # v10.7: atomic snapshot write
                state["positions"] = positions
                if new_prices is not None:
                    state["prices"] = new_prices
                state["orders"] = new_orders
                state["reconciliation"] = reconcile_broker_state(positions, new_orders, [])
                state["last_updated"] = datetime.now().isoformat()
            log.info(f"ðŸ“Š Loaded {len(positions)} positions, {len(state['orders'])} recent orders")

            # Start WebSocket streaming
            all_tickers = list(set(tickers + list(DEMO_PRICES.keys())))
            streamer = PriceStreamer(all_tickers, on_live_price)
            streamer.start()
        else:
            log.error("âŒ Could not connect to Alpaca â€” check your API keys")
            log.info("   Running in DEMO mode")
            state["mode"] = "demo"

    log.info(f"\nðŸš€ Mode: {'ðŸ“„ PAPER TRADING' if state['mode']=='paper' else 'ðŸŽ® DEMO'}")
    log.info(f"ðŸŒ API: http://localhost:5001")
    log.info(f"   /all  /positions  /prices  /account  /orders  /buy  /sell")
    log.info("="*52 + "\n")

    # Start REST polling background thread
    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    startup()
    app.run(host=os.getenv("KTRADE_BIND_HOST", "127.0.0.1"),
            port=int(os.getenv("KTRADE_PORT", "5001")), debug=False, use_reloader=False)


