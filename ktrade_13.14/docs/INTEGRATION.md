# KTrade v10.0 — Integration Guide

Five changes, ordered by leverage. Each is written to drop into the v9.1 tree;
adapt names to your actual client/tracker interfaces.

---

## 1. Build closed trades from real fills (the core fix)

Replace any logic that closes a position at the last quote/mark with the reconciler.
Call it once per cycle (or on a slower timer, e.g. every 60s) and after any exit.

```python
from broker_reconciler import BrokerReconciler

reconciler = BrokerReconciler(API_KEY, SECRET_KEY, base_url=PAPER_BASE,
                              max_desyncs_before_halt=3)

# agent_open_book: symbol -> signed qty from YOUR in-memory position tracker
result = reconciler.reconcile(agent_open_book=position_tracker.snapshot())

# result.round_trips all have verified=True and REAL exit prices.
# Persist these as your closed-trade ledger; your win-rate / profit-factor now
# reflect economics, not reconciliation noise.
for rt in result.round_trips:
    ledger.record_closed(rt)
```

Key point: there is no "exclude from performance" category anymore. A trade either
has a real fill (it's in `round_trips`) or it's a desync (it's a bug, see #5).

---

## 2. client_order_id on every order

This is what lets you map agent intent to broker fills deterministically and detect
orphans vs duplicates. Without it, desyncs are unresolvable.

```python
import uuid

def submit_entry(symbol, qty, side):
    coid = f"ktrade-{symbol}-{side}-{uuid.uuid4().hex[:12]}"
    order = alpaca.submit_order(
        symbol=symbol, qty=qty, side=side, type="market",
        time_in_force="day", client_order_id=coid,
    )
    position_tracker.register(symbol, qty, side, coid, order.id)
    return order
```

Store `coid` alongside the position in your tracker so reconciliation can join on it.

---

## 3. Rebuild the open book from the broker on startup / crash recovery

Your v9.1 already has crash resilience + backoff. Extend recovery so it trusts the
broker, not the local state file.

```python
def recover_state(alpaca, position_tracker):
    broker_positions = alpaca.list_positions()      # /v2/positions
    open_orders = alpaca.list_orders(status="open") # /v2/orders?status=open
    position_tracker.reset()
    for p in broker_positions:
        position_tracker.register_from_broker(p.symbol, float(p.qty), float(p.avg_entry_price))
    position_tracker.attach_open_orders(open_orders)  # see #4
    # Now the in-memory book == broker reality. Never start from a stale file.
```

Run this at boot AND after any reconnect/401-reauth event.

---

## 4. Persist bracket/OCO leg order IDs

You already pre-stage resting brackets at the broker (good — exits survive a crash).
Persist the leg IDs so recovery re-attaches instead of either thinking the position
is unprotected or submitting a duplicate exit.

```python
# When placing the bracket:
bracket = alpaca.submit_order(
    symbol=symbol, qty=qty, side="buy", type="market", time_in_force="day",
    order_class="bracket",
    take_profit={"limit_price": target},
    stop_loss={"stop_price": stop},
    client_order_id=coid,
)
# bracket.legs -> [take_profit_leg, stop_loss_leg]
position_tracker.save_legs(symbol, coid,
                           [leg.id for leg in (bracket.legs or [])])

# On recovery, attach_open_orders() matches these leg IDs back to the position
# so you don't double-arm exits.
```

---

## 5. Desync = alarm + circuit breaker (not a silent footnote)

A desync means your book was wrong. That's a bigger problem than a small loss.
Hook it into the same kill path as DailyLossGuard.

```python
result = reconciler.reconcile(agent_open_book=position_tracker.snapshot())

for d in result.desyncs:
    log.error("DESYNC %s: agent=%s broker=%s — %s",
              d.symbol, d.agent_qty, d.broker_qty, d.reason)

if reconciler.should_halt(result):
    kill_switch.trip(reason="position-state desync — book unreliable")
    # stop new entries; let resting broker brackets manage existing exits.
```

This mirrors your existing consecutive-loss halt, but for state integrity instead
of P&L. The point: never trade on a book you can't trust.

---

## Verification checklist before going live (paper)
- [ ] `client_order_id` present on 100% of submitted orders
- [ ] Closed-trade ledger built only from `verified=True` round-trips
- [ ] `recover_state()` runs at boot and after reauth
- [ ] Bracket leg IDs persisted and re-attached on recovery
- [ ] A forced desync (manually close a position in Alpaca) trips the halt
