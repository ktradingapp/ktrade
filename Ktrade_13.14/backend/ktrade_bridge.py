"""
KTrade — Robinhood Data Bridge
================================
Fetches your real Robinhood positions + live prices
and serves them to the KTrade frontend via a local API.

SETUP:
  pip install robin_stocks flask flask-cors

RUN:
  python ktrade_bridge.py

Then open KTrade and it pulls your REAL data.
"""

import os
import time
import json
import threading
from datetime import datetime
from flask import Flask, jsonify
from flask_cors import CORS

# ── Try importing robin_stocks ──────────────────────────────────────────────
try:
    import robin_stocks.robinhood as rh
    ROBIN_AVAILABLE = True
except ImportError:
    ROBIN_AVAILABLE = False
    print("⚠  robin_stocks not installed. Run: pip install robin_stocks")
    print("   Running in DEMO mode with simulated data.\n")

# ── Config ───────────────────────────────────────────────────────────────────
RH_USERNAME = os.environ.get("RH_USERNAME", "")   # set in env or paste here
RH_PASSWORD = os.environ.get("RH_PASSWORD", "")   # set in env or paste here
REFRESH_SECONDS = 30                               # how often to refresh prices

# ── Demo fallback data (used when not logged in) ─────────────────────────────
DEMO_POSITIONS = [
    {"ticker": "NVDA",  "name": "NVIDIA Corp",    "shares": 10, "avgCost": 118.50, "sector": "AI Compute"},
    {"ticker": "TSLA",  "name": "Tesla Inc",       "shares": 15, "avgCost": 242.00, "sector": "EV/AI"},
    {"ticker": "QQQ",   "name": "Invesco QQQ",     "shares": 20, "avgCost": 450.00, "sector": "ETF"},
    {"ticker": "AAPL",  "name": "Apple Inc",       "shares": 12, "avgCost": 188.00, "sector": "Tech"},
    {"ticker": "MSFT",  "name": "Microsoft Corp",  "shares": 8,  "avgCost": 390.00, "sector": "AI/Cloud"},
    {"ticker": "GOOGL", "name": "Alphabet Inc",    "shares": 5,  "avgCost": 168.00, "sector": "AI/Ads"},
    {"ticker": "ORCL",  "name": "Oracle Corp",     "shares": 6,  "avgCost": 142.00, "sector": "Cloud"},
    {"ticker": "IBM",   "name": "IBM Corp",        "shares": 9,  "avgCost": 195.00, "sector": "Enterprise"},
]

DEMO_PRICES = {
    "NVDA": 131.20, "TSLA": 228.40, "QQQ": 478.60,
    "AAPL": 195.30, "MSFT": 412.80, "GOOGL": 172.50,
    "ORCL": 155.20, "IBM": 208.40
}

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "logged_in": False,
    "positions": [],
    "prices": {},
    "account": {},
    "last_updated": None,
    "mode": "demo"
}

# ── Robinhood Login ───────────────────────────────────────────────────────────
def login():
    if not ROBIN_AVAILABLE:
        print("📊 Demo mode active (robin_stocks not installed)")
        return False

    if not RH_USERNAME or not RH_PASSWORD:
        print("📊 No credentials found — running in demo mode")
        print("   Set RH_USERNAME and RH_PASSWORD environment variables to connect Robinhood")
        return False

    try:
        print(f"🔐 Logging in to Robinhood as {RH_USERNAME}...")
        # MFA-aware login — will prompt in terminal if needed
        rh.login(
            username=RH_USERNAME,
            password=RH_PASSWORD,
            expiresIn=86400,       # 24 hour session
            store_session=True     # caches token so you don't re-login every time
        )
        print("✅ Robinhood login successful!")
        return True
    except Exception as e:
        print(f"❌ Login failed: {e}")
        print("   Running in demo mode instead.")
        return False

# ── Fetch Real Positions ──────────────────────────────────────────────────────
def fetch_positions():
    if not state["logged_in"]:
        return DEMO_POSITIONS, DEMO_PRICES

    try:
        print("📡 Fetching positions from Robinhood...")
        raw = rh.account.build_holdings()
        positions = []
        tickers = []

        for ticker, data in raw.items():
            positions.append({
                "ticker":   ticker,
                "name":     data.get("name", ticker),
                "shares":   float(data.get("quantity", 0)),
                "avgCost":  float(data.get("average_buy_price", 0)),
                "sector":   data.get("type", "Stock"),
                "equity":   float(data.get("equity", 0)),
                "pe_ratio": data.get("pe_ratio", "N/A"),
            })
            tickers.append(ticker)

        # Fetch live quotes
        prices = {}
        if tickers:
            quotes = rh.stocks.get_latest_price(tickers)
            for i, ticker in enumerate(tickers):
                try:
                    prices[ticker] = float(quotes[i]) if quotes[i] else 0
                except:
                    prices[ticker] = 0

        print(f"✅ Fetched {len(positions)} positions")
        return positions, prices

    except Exception as e:
        print(f"⚠  Error fetching positions: {e} — using last known data")
        return state["positions"] or DEMO_POSITIONS, state["prices"] or DEMO_PRICES

# ── Fetch Account Summary ─────────────────────────────────────────────────────
def fetch_account():
    if not state["logged_in"]:
        total = sum(DEMO_PRICES.get(p["ticker"], p["avgCost"]) * p["shares"] for p in DEMO_POSITIONS)
        cost  = sum(p["avgCost"] * p["shares"] for p in DEMO_POSITIONS)
        return {
            "total_value":    round(total, 2),
            "total_cost":     round(cost, 2),
            "buying_power":   1250.00,
            "total_return":   round(total - cost, 2),
            "return_pct":     round((total - cost) / cost * 100, 2),
            "mode":           "demo"
        }

    try:
        profile = rh.profiles.load_account_profile()
        portfolio = rh.profiles.load_portfolio_profile()
        return {
            "total_value":   float(portfolio.get("equity", 0)),
            "buying_power":  float(profile.get("buying_power", 0)),
            "withdrawable":  float(profile.get("cash_available_for_withdrawal", 0)),
            "total_return":  float(portfolio.get("total_return", 0)),
            "return_pct":    float(portfolio.get("portfolio_return_today", 0)) * 100,
            "mode":          "live"
        }
    except Exception as e:
        print(f"⚠  Account fetch error: {e}")
        return {}

# ── Background Refresh Loop ───────────────────────────────────────────────────
def refresh_loop():
    while True:
        try:
            positions, prices = fetch_positions()
            account = fetch_account()
            state["positions"]    = positions
            state["prices"]       = prices
            state["account"]      = account
            state["last_updated"] = datetime.now().isoformat()
            print(f"🔄 Refreshed at {state['last_updated']}")
        except Exception as e:
            print(f"⚠  Refresh error: {e}")
        time.sleep(REFRESH_SECONDS)

# ── Flask API ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # allows your browser/React app to call this

@app.route("/")
def index():
    return jsonify({
        "service": "KTrade Robinhood Bridge",
        "version": "1.0",
        "mode":    state["mode"],
        "status":  "running",
        "endpoints": ["/positions", "/prices", "/account", "/all", "/health"]
    })

@app.route("/health")
def health():
    return jsonify({
        "ok":           True,
        "logged_in":    state["logged_in"],
        "mode":         state["mode"],
        "last_updated": state["last_updated"],
        "position_count": len(state["positions"])
    })

@app.route("/positions")
def positions():
    return jsonify({
        "positions":    state["positions"],
        "last_updated": state["last_updated"],
        "mode":         state["mode"]
    })

@app.route("/prices")
def prices():
    return jsonify({
        "prices":       state["prices"],
        "last_updated": state["last_updated"]
    })

@app.route("/account")
def account():
    return jsonify(state["account"])

@app.route("/all")
def all_data():
    """Single endpoint — KTrade frontend calls this to get everything"""
    return jsonify({
        "positions":    state["positions"],
        "prices":       state["prices"],
        "account":      state["account"],
        "last_updated": state["last_updated"],
        "mode":         state["mode"],
        "logged_in":    state["logged_in"]
    })

# ── Quote lookup for a specific ticker ───────────────────────────────────────
@app.route("/quote/<ticker>")
def quote(ticker):
    ticker = ticker.upper()
    if state["logged_in"] and ROBIN_AVAILABLE:
        try:
            price = rh.stocks.get_latest_price(ticker)
            return jsonify({"ticker": ticker, "price": float(price[0])})
        except:
            pass
    # fallback to cached or demo
    price = state["prices"].get(ticker) or DEMO_PRICES.get(ticker, 0)
    return jsonify({"ticker": ticker, "price": price, "source": "cache"})

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "="*52)
    print("   KTrade — Robinhood Bridge")
    print("="*52)

    # Try Robinhood login
    state["logged_in"] = login()
    state["mode"] = "live" if state["logged_in"] else "demo"

    # Initial data fetch
    print("\n📊 Loading initial data...")
    positions, prices = fetch_positions()
    state["positions"] = positions
    state["prices"]    = prices
    state["account"]   = fetch_account()
    state["last_updated"] = datetime.now().isoformat()

    print(f"\n✅ Running in {'LIVE' if state['logged_in'] else 'DEMO'} mode")
    print(f"   Positions loaded: {len(state['positions'])}")
    print(f"   Refreshing every: {REFRESH_SECONDS}s")
    print(f"\n🌐 API running at: http://localhost:5001")
    print(f"   Endpoints: /all  /positions  /prices  /account  /health")
    print("="*52 + "\n")

    # Start background refresh thread
    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()

    # Start Flask
    app.run(host="0.0.0.0", port=5001, debug=False)
