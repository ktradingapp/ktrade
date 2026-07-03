# KTrade PRO — Action Plan & Development Roadmap
### Version 1.0 | iT LLC

---

## Phase 1 — COMPLETED ✅ (Current State)

### 1.1 Frontend Dashboard
- [x] React-based trading dashboard (KTrade PRO branding)
- [x] 5 tabs: Signals, Portfolio, Alerts, Performance, AI Advisor
- [x] Live price simulation (2.5s tick)
- [x] Options signals with conviction scoring (color-coded rings)
- [x] Tap-to-expand signal cards with rationale + R/R ratio
- [x] Sparkline charts per position
- [x] Real-time P&L tracking
- [x] Price alerts with auto-fire toast notifications
- [x] Performance tab: Portfolio vs S&P 500 YTD bar chart
- [x] Signal history with W/L badges
- [x] AI Advisor tab powered by Claude API

### 1.2 Backend — Broker Bridges
- [x] Alpaca paper trading bridge (PRIMARY)
  - Real position fetching
  - Live price snapshots
  - WebSocket streaming (real-time quotes)
  - Buy/Sell/Cancel order endpoints
  - REST polling fallback (30s)
  - Demo mode (no credentials needed)
- [x] Robinhood bridge (unofficial, via robin_stocks)
  - Position fetching
  - Price polling
  - Demo fallback

### 1.3 Risk Management Engine
- [x] Hard-coded kill switch (manual + auto)
- [x] Max Daily Drawdown guard (3% default)
- [x] Daily loss dollar cap
- [x] VIX circuit breakers (risk-off at 30, close-all at 50)
- [x] Flash crash detection (SPY -2.5% in 10min)
- [x] ATR-based dynamic position sizing
- [x] Kelly Criterion position sizing (25% fractional)
- [x] Broker-side bracket orders (stop + target simultaneously)
- [x] Options spread check (reject wide spreads)
- [x] Conviction gate (reject signals < 75)
- [x] **FIX: Duplicate order prevention** (60s window)
- [x] **FIX: Same-ticker day limit** (max 3 entries/day)
- [x] **FIX: Post-fill cooldown** (5 min mandatory wait)
- [x] **FIX: Short/hedge tracking** (15% exposure cap)
- [x] Auto-hedge warning when VIX > 28

---

## Phase 2 — IN PROGRESS 🔄

### 2.1 Real Data Integration
- [ ] Connect KTrade frontend to Alpaca bridge (http://localhost:5001/all)
- [ ] Replace simulated prices with live Alpaca WebSocket feed
- [ ] Real positions from Alpaca paper account
- [ ] Real order placement from dashboard Buy/Sell buttons
- [ ] Order status tracking (pending → filled → closed)

### 2.2 Signal Engine
- [ ] Connect to real options flow data (Unusual Whales / Market Chameleon API)
- [ ] MACD + EMA confluence signal generator
- [ ] Opening Range Breakout (ORB) scanner
- [ ] Candlestick pattern recognition module
- [ ] ConvictionScorer across 200 ticker universe

### 2.3 Alerts Enhancement
- [ ] SMS alerts via Twilio
- [ ] Email alerts via SendGrid
- [ ] Push notifications (mobile PWA)
- [ ] Webhook support (Discord, Slack)

---

## Phase 3 — PLANNED 📋

### 3.1 Autonomous Agent
- [ ] Heartbeat scheduler (market hours awareness)
- [ ] Auto-scan top 200 tickers every 5 minutes
- [ ] Auto-execute approved signals (Risk Engine gated)
- [ ] Position monitor — auto-exit at stop/target
- [ ] Daily P&L report generation
- [ ] Crash detection pre-event agent (VIX, put/call, sector rotation)

### 3.2 Advanced Risk Features
- [ ] Sector concentration cap (max 30% in any sector)
- [ ] Correlation filter (avoid 2 highly correlated positions)
- [ ] IV rank filter (only enter when IV rank < 50)
- [ ] Time-of-day filter (avoid first/last 15 min)
- [ ] Earnings blackout window (no entries 3 days before earnings)

### 3.3 Performance Analytics
- [ ] Full trade journal with forensic loss recaps
- [ ] Sharpe ratio, max drawdown, win rate tracking
- [ ] Per-strategy performance breakdown
- [ ] Benchmark comparison (vs SPY, QQQ, sector ETFs)
- [ ] Monthly PDF report generation

### 3.4 Pilot Marketplace
- [ ] Follow/mirror external signal sources
- [ ] Politician 13F tracker (Pelosi, congressional disclosures)
- [ ] Hedge fund 13F mirror (Citadel, Druckenmiller)
- [ ] Krishna Sumanth ORB signal integration

---

## Immediate Next Actions (This Week)

| Priority | Task | File | Effort |
|----------|------|------|--------|
| 🔴 HIGH | Connect frontend to Alpaca bridge | KTrade_live.jsx | 2h |
| 🔴 HIGH | Add Buy/Sell buttons to portfolio tab | KTrade_live.jsx | 1h |
| 🟡 MED | Wire Risk Engine into Alpaca bridge | ktrade_alpaca.py + ktrade_risk.py | 3h |
| 🟡 MED | Add Twilio SMS for price alerts | ktrade_alpaca.py | 1h |
| 🟢 LOW | Add 50-ticker watchlist from Krishna | New file | 2h |
| 🟢 LOW | Build ORB signal scanner | New file | 4h |

---

## Known Issues / Tech Debt

1. **Frontend uses simulated prices** — not yet connected to Alpaca WebSocket
2. **No authentication** — dashboard is open, needs login for production
3. **Risk engine not wired to Alpaca bridge** — currently separate modules
4. **Robinhood bridge is unofficial** — may break if Robinhood changes their API
5. **No persistent storage** — signals and trade history reset on restart

---

## Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React (JSX), Tailwind-adjacent inline CSS |
| AI Advisor | Claude Sonnet 4.6 (Anthropic API) |
| Backend | Python + Flask + Flask-CORS |
| Broker | Alpaca Markets API (paper + live) |
| Real-time | Alpaca WebSocket (IEX feed) |
| Risk Engine | Pure Python — deterministic math only |
| Hosting | Local (dev) → plan for VPS/cloud deployment |
