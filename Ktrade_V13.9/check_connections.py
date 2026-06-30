r"""
KTrade Connection Checker v2
Run: .venv\Scripts\python check_connections.py
"""
import os, sys, json
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests"); sys.exit(1)

# Load .env manually from project root
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    print(f"✅ .env loaded from {env_file}")
else:
    print(f"⚠ .env not found at {env_file}")

key    = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_KEY","")
secret = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET","")
poly   = os.getenv("POLYGON_KEY","")
headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

print(f"   APCA_API_KEY_ID:     {'SET ✅ '+key[:8]+'...' if key else 'NOT SET ❌'}")
print(f"   APCA_API_SECRET_KEY: {'SET ✅' if secret else 'NOT SET ❌'}")
print(f"   POLYGON_KEY:         {'SET ✅ '+poly[:8]+'...' if poly else 'NOT SET'}")

print("\n" + "="*50)
print("  KTrade Connection Check")
print("="*50)

# 1. Alpaca Account — via backend
print("\n[1] Alpaca Account (via backend)")
try:
    r = requests.get("http://localhost:5001/account", timeout=5)
    a = r.json()
    equity = float(a.get("equity", 0))
    cash   = float(a.get("cash", 0))
    bp     = float(a.get("buying_power", 0))
    status = a.get("status","?")
    print(f"    OK  status={status}")
    print(f"    equity=${equity:,.0f}  cash=${cash:,.0f}  buying_power=${bp:,.0f}")
except Exception as e:
    # Try direct Alpaca API
    try:
        r = requests.get(
            "https://paper-api.alpaca.markets/v2/account",
            headers=headers, timeout=5
        )
        a = r.json()
        if "equity" in a:
            print(f"    OK  equity=${float(a['equity']):,.0f}  status={a.get('status')}")
        else:
            print(f"    FAIL  {a.get('message','unknown error')}")
            print(f"    TIP: Check APCA_API_KEY_ID in your .env file")
    except Exception as e2:
        print(f"    FAIL  {e2}")

# 2. Alpaca Live Prices — via backend
print("\n[2] Alpaca Live Prices")
try:
    r = requests.get("http://localhost:5001/prices", timeout=5)
    prices = r.json()
    nvda = prices.get("NVDA", prices.get("prices",{}).get("NVDA",0))
    spy  = prices.get("SPY",  prices.get("prices",{}).get("SPY",0))
    qqq  = prices.get("QQQ",  prices.get("prices",{}).get("QQQ",0))
    if nvda:
        print(f"    OK  NVDA=${nvda:.2f}  SPY=${spy:.2f}  QQQ=${qqq:.2f}")
        print(f"    Source: Alpaca real-time (via backend)")
    else:
        print(f"    Backend returned: {str(prices)[:100]}")
except Exception as e:
    print(f"    FAIL  {e}")

# 3. WebSocket Library
print("\n[3] WebSocket Streaming")
try:
    import websocket
    print(f"    OK  websocket-client installed")
except ImportError:
    print(f"    MISSING  run: .venv\\Scripts\\pip install websocket-client")

# 4. Polygon API
print("\n[4] Polygon API")
if poly:
    try:
        r = requests.get(
            "https://api.polygon.io/v2/aggs/ticker/NVDA/range/1/day/2025-01-01/2025-01-02",
            params={"apiKey": poly}, timeout=5
        )
        if r.status_code == 200:
            results = r.json().get("resultsCount", 0)
            print(f"    OK  Connected  results={results}")
        elif r.status_code == 403:
            print(f"    LIMITED  Free tier active (price data OK, options blocked)")
        else:
            print(f"    status={r.status_code}")
    except Exception as e:
        print(f"    FAIL  {e}")
else:
    print(f"    NOT SET  Using yfinance for data")

# 5. Backend Health
print("\n[5] Backend (localhost:5001)")
try:
    r = requests.get("http://localhost:5001/health", timeout=3)
    d = r.json()
    print(f"    OK  connected={d.get('connected')}  mode={d.get('mode')}")
    print(f"    positions={d.get('positions',0)}  errors={len(d.get('errors',[]))}")
except Exception:
    print(f"    NOT RUNNING  start: .venv\\Scripts\\python backend\\ktrade_alpaca.py")

# 6. Scan Results
print("\n[6] Latest Scan Results")
scan = Path("data") / "ktrade_scan_latest.json"
if scan.exists():
    data = json.loads(scan.read_text(encoding="utf-8"))
    results = data.get("results", [])
    t = data.get("scan_time", data.get("timestamp","?"))[:19]
    buys = [r for r in results if (r.get("conviction",r.get("score",0)))>=75]
    watches=[r for r in results if 60<=(r.get("conviction",r.get("score",0)))<75]
    print(f"    OK  {len(results)} signals  ({len(buys)} BUY, {len(watches)} WATCH)")
    print(f"    saved: {t}")
else:
    print(f"    MISSING  run: .venv\\Scripts\\python agent\\ktrade_agent_v9.py --score-only")

# 7. VectorBT Params
print("\n[7] VectorBT Approved Params")
params_file = Path("data") / "ktrade_approved_params.json"
if params_file.exists():
    params = json.loads(params_file.read_text(encoding="utf-8"))
    approved = sum(1 for t in params.values() for s in t.values() if s.get("approved"))
    print(f"    OK  {approved} approved strategies across {len(params)} tickers")
    # Show top 3
    for ticker, strats in list(params.items())[:3]:
        ap = [s for s,v in strats.items() if v.get("approved")]
        if ap:
            print(f"    {ticker}: {', '.join(ap)}")
else:
    print(f"    MISSING  run: .venv\\Scripts\\python ktrade_vectorbt.py")

print("\n" + "="*50 + "\n")
