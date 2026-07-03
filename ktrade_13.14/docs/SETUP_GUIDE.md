# KTrade PRO — Setup Guide
### Step-by-Step from Zero to Paper Trading

---

## Prerequisites

- Python 3.9+ installed
- Node.js 18+ (only if running React app locally)
- A free Alpaca Markets account
- Claude API access (for AI Advisor)

---

## Step 1 — Install Python Dependencies

```bash
pip install requests flask flask-cors websocket-client robin_stocks
```

Verify:
```bash
python -c "import flask, requests; print('OK')"
```

---

## Step 2 — Get Alpaca Paper Trading Keys

1. Go to **https://alpaca.markets** → Sign Up (free)
2. After login → click **"Paper Trading"** in left sidebar
3. Go to **API Keys** → **Generate New Key**
4. Copy:
   - **Key ID**: starts with `PK...`
   - **Secret Key**: long alphanumeric string

> ⚠️ Never commit keys to GitHub. Always use environment variables.

---

## Step 3 — Set Environment Variables

**Mac/Linux:**
```bash
export ALPACA_KEY="PKxxxxxxxxxxxxxxxxxx"
export ALPACA_SECRET="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

**Windows:**
```cmd
set ALPACA_KEY=PKxxxxxxxxxxxxxxxxxx
set ALPACA_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Permanent (add to ~/.zshrc or ~/.bashrc):**
```bash
echo 'export ALPACA_KEY="PKxxx..."' >> ~/.zshrc
echo 'export ALPACA_SECRET="xxx..."' >> ~/.zshrc
source ~/.zshrc
```

---

## Step 4 — Run the Alpaca Bridge

```bash
cd ktrade_v1.0/backend
python ktrade_alpaca.py
```

Expected output:
```
✅ Connected! Equity: $100,000.00 | Buying power: $200,000.00
📊 Loaded 0 positions, 0 recent orders
🚀 Mode: 📄 PAPER TRADING
🌐 API: http://localhost:5001
```

---

## Step 5 — Test the API Endpoints

Open in browser or run curl:

```bash
# Check connection
curl http://localhost:5001/health

# See your paper account
curl http://localhost:5001/account

# Your positions (empty at start)
curl http://localhost:5001/positions

# Place a paper trade (buy 5 NVDA)
curl -X POST http://localhost:5001/buy \
  -H "Content-Type: application/json" \
  -d '{"ticker":"NVDA","qty":5,"type":"market"}'

# Check your positions again
curl http://localhost:5001/positions
```

---

## Step 6 — Open the Dashboard

Option A — Static preview (instant):
```bash
open ktrade_v1.0/frontend/KTrade_preview.html
```

Option B — Live React artifact:
- Open Claude.ai
- Upload `KTrade_live.jsx`
- Ask Claude to "run this as an artifact"

Option C — Local React dev server:
```bash
npx create-react-app ktrade-app
cp frontend/KTrade_live.jsx ktrade-app/src/App.jsx
cd ktrade-app && npm start
```

---

## Step 7 — Run the Risk Engine (Optional Standalone Test)

```bash
cd ktrade_v1.0/risk
python ktrade_risk.py
```

Expected output shows all 3 fixes working:
```
FIX 1: LITE order #1 APPROVED, orders #2-4 BLOCKED (duplicate)
FIX 2: APLD re-entry BLOCKED (cooldown active)
FIX 3: NVDA approved with VIX hedge warning
```

---

## Step 8 — Wire Risk Engine to Alpaca Bridge

In `ktrade_alpaca.py`, the risk engine integration point is the `/buy` endpoint.
Add this before placing any order:

```python
from ktrade_risk import RiskEngine, TradeRequest

engine = RiskEngine(account_equity=100_000)

@app.route("/buy", methods=["POST"])
def buy():
    body = request.get_json()
    
    # Risk check FIRST — before any broker call
    trade = TradeRequest(
        ticker=body["ticker"],
        side="buy",
        qty=body["qty"],
        price=body.get("price", 0),
        conviction=body.get("conviction", 80)
    )
    decision = engine.evaluate(trade)
    
    if not decision.approved:
        return jsonify({"blocked": True, "reason": decision.reason}), 403
    
    # Only reaches here if risk engine approved
    result = place_order(body["ticker"], decision.approved_qty, "buy")
    engine.record_fill(body["ticker"], "buy", decision.approved_qty, body.get("price", 0))
    return jsonify({"ok": True, "order": result, "risk": {"stop": decision.stop_price, "target": decision.target_price}})
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError: flask` | `pip install flask flask-cors` |
| `Connection refused localhost:5001` | Make sure `ktrade_alpaca.py` is running |
| Alpaca returns 403 | Check your API keys are set correctly |
| "Paper trading only" warning | You're on paper endpoint — this is correct for testing |
| WebSocket not connecting | `pip install websocket-client` |
| Port 5001 in use | Change port in `ktrade_alpaca.py` → `app.run(port=5002)` |

---

## File Reference

| File | Purpose | Run? |
|------|---------|------|
| `backend/ktrade_alpaca.py` | Main broker bridge | `python ktrade_alpaca.py` |
| `backend/ktrade_bridge.py` | Robinhood fallback | `python ktrade_bridge.py` |
| `risk/ktrade_risk.py` | Risk engine | Import as module or run to demo |
| `frontend/KTrade_live.jsx` | React dashboard | Claude artifact or local React |
| `frontend/KTrade_preview.html` | Static preview | Open in browser |
