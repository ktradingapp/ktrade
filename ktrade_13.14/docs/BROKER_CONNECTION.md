# KTrade PRO — Broker Connection Guide
### Alpaca (Primary) + Robinhood (Fallback)

---

## Option 1: Alpaca Markets (RECOMMENDED)

### Why Alpaca?
- Free official API — no reverse engineering
- Paper trading built-in ($100k fake money)
- WebSocket real-time streaming
- Bracket orders supported (stop + target server-side)
- Supports stocks + crypto (options coming)
- Switches seamlessly from paper → live

### Paper Trading URLs
```
Base:    https://paper-api.alpaca.markets
Data:    https://data.alpaca.markets
Stream:  wss://stream.data.alpaca.markets/v2/iex
```

### Live Trading URLs (when ready)
```
Base:    https://api.alpaca.markets
Data:    https://data.alpaca.markets
Stream:  wss://stream.data.alpaca.markets/v2/iex
```

To switch from paper to live, change ONE line in `ktrade_alpaca.py`:
```python
# Paper
BASE_URL = "https://paper-api.alpaca.markets"

# Live (when ready — use real money carefully!)
BASE_URL = "https://api.alpaca.markets"
```

### What Alpaca Can Do
| Feature | Paper | Live |
|---------|-------|------|
| Read positions | ✅ | ✅ |
| Live prices | ✅ | ✅ |
| Place market orders | ✅ | ✅ |
| Place limit orders | ✅ | ✅ |
| Bracket orders | ✅ | ✅ |
| Options trading | ❌ | ❌ (coming) |
| WebSocket streaming | ✅ | ✅ |
| Account balance | ✅ | ✅ |

### API Endpoints We Use
```
GET  /v2/account              → equity, buying power, status
GET  /v2/positions            → all open positions
GET  /v2/orders?status=all    → order history
POST /v2/orders               → place new order
DELETE /v2/orders/{id}        → cancel order
GET  /v2/stocks/snapshots     → live price snapshots (data URL)
WSS  /v2/iex                  → real-time quote stream
```

---

## Option 2: Robinhood (Unofficial Fallback)

### Why Robinhood?
- You already have an account with real positions
- Supports options (unlike Alpaca currently)
- Shows your real portfolio

### Why It's Risky
- **No official API** — uses reverse-engineered endpoints
- Can break anytime Robinhood updates their app
- Robinhood may flag unusual API usage
- MFA required every session

### Setup
```bash
pip install robin_stocks

export RH_USERNAME="your@email.com"
export RH_PASSWORD="yourpassword"

python backend/ktrade_bridge.py
```

### What Robinhood Bridge Can Do
| Feature | Status |
|---------|--------|
| Read positions | ✅ |
| Read prices | ✅ (polling only, no WebSocket) |
| Read options positions | ✅ |
| Place orders | ⚠️ (works but unofficial) |
| Bracket orders | ❌ (not supported) |
| WebSocket streaming | ❌ |

### MFA Handling
First run will prompt in terminal:
```
Enter MFA code: 123456
```
After that, session is cached for 24 hours.

---

## Option 3: TD Ameritrade / Schwab

Coming in v1.1 — Schwab acquired TDA and has an official API.

---

## Option 4: Interactive Brokers

Coming in v1.2 — IBKR has the most powerful API (TWS API) but
requires more setup. Best for institutional-grade execution.

---

## Switching Between Brokers

The KTrade backend is designed so the frontend doesn't care which
broker is connected — it always calls `http://localhost:5001/all`.

To switch brokers, just run a different backend script:

```bash
# Use Alpaca (recommended)
python backend/ktrade_alpaca.py

# Use Robinhood (fallback)
python backend/ktrade_bridge.py
```

The frontend auto-detects which mode is running via the `/health` endpoint.

---

## Security Best Practices

1. **Never hardcode credentials** — always use environment variables
2. **Never commit .env files** to GitHub
3. **Paper trade first** — minimum 2 weeks before going live
4. **Alpaca IP whitelist** — restrict API access to your IP in Alpaca settings
5. **Daily key rotation** — regenerate API keys monthly
6. **Monitor the bridge logs** — watch for unexpected order activity

---

## Rate Limits

| API | Limit |
|-----|-------|
| Alpaca REST | 200 requests/min |
| Alpaca WebSocket | Unlimited (streaming) |
| Alpaca Data snapshots | 200 symbols per request |
| Robinhood (unofficial) | ~50 req/min before throttle |
