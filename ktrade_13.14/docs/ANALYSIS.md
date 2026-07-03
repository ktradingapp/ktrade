# KTrade v10.0 — Analysis & Findings

Reference write-up of what was diagnosed from a peer's Alpaca paper-trading system
("TradeRadar") and what it means for KTrade. Source artifacts are in `../reference/`.

---

## 1. What TradeRadar is

A read-only monitoring dashboard (single HTML page, vanilla JS) on top of an Alpaca
paper account, hosted on a VPS. Alpaca keys live server-side; the page proxies through
`/api`. It is a **viewer, not the agent** — the trading logic runs server-side.

Data flow (from the page's own JS):

```
/v2/account, /v2/positions, /v2/orders   -> proxied to Alpaca (broker truth)
/agent/trades                            -> the agent's own trade log
/agent/closed_trades                     -> "signed round-trip engine" (server-side)
```

The dashboard labels `/agent/closed_trades` the "single source of truth." All
reconciliation logic lives in that server endpoint, NOT in the HTML.

Watchlist architecture mirrors KTrade: themed universe (AI Energy, Compute, Infra,
Cloud, App, Quantum, Robotics, Space, Thematic ETFs, Big-Drop Watch), each tagged with
a strategy mode. Exits are reason-coded (TIER_1/TIER_2, r0 hard stop, broker flat
reconcile).

## 2. The data-quality finding

Top-line numbers on the dashboard (net P&L ~ -$2,308, profit factor 0.75, win rate
~51% with avg win $49 vs avg loss $68) are **contaminated by reconciliation artifacts**.

The system itself, in the trade-detail popup, states that any row tagged
**"broker flat reconcile"** is *"not a real economic trade; exclude from performance"* —
because the exit price is a synthetic last-quote mark, not a real fill.

Implication: do not trust that dashboard's headline P&L. True performance requires
filtering to genuine exits only (real fills / hard stops) and dropping every reconcile
row.

## 3. Root cause

The agent keeps its own open-position book. Each cycle it compares that book to
`/v2/positions`. When its book says "open" but the broker shows flat — with no matching
exit fill found — it closes the lot at the last mark and stamps it "broker flat
reconcile." This is **position-state drift between the agent and the broker**, papered
over after the fact. Common triggers: crash/restart losing in-memory state, exit legs
filling/expiring unrecorded, paper-account auto-liquidation, or polling gaps.

The volume of reconcile rows is itself the red flag: it means the book is frequently
wrong. A system that can't reliably know what it holds is a bigger problem than one
that loses a little money.

## 4. The RKLB example (see reference/RKLB_trade_detail_popup.png)

- Signal: `deterministic_volatility_momentum` — a breakout chaser.
- Entry: bought 40 @ $107.62 AFTER a +5.21% same-day move (6.28% intraday range).
- Risk plan: stop $104.46, target $138.25 — a ~10R target vs a ~3% stop.
- Outcome: "broker flat reconcile" at $104.65 after 17.9h, -$118.80 (-2.76%),
  flagged by the system as a reconciliation artifact, not a real exit.

A wide-target / tight-stop momentum chaser bleeds through chop. KTrade's existing
riskguard (entry windows, no-averaging, structural stops, payoff-ratio awareness) is
the right complement — the strategy layer does not need to change to avoid this.

## 5. What KTrade should change (state integrity)

1. Source closed trades from real FILL activities, not last-quote marks.
2. Stamp every order with a `client_order_id` (intent <-> fill mapping).
3. On startup / post-crash, rebuild the open book from the broker.
4. Persist bracket/OCO leg order IDs so recovery re-attaches instead of orphaning.
5. Treat a desync as an alarm + circuit breaker, not a silent exclusion.

Full wiring with code is in `../INTEGRATION.md`; the module is `../broker_reconciler.py`.

---

*All observations are system-engineering notes on a paper account, not a view on what
to trade.*
