"""
broker_adapter.py - KTrade v10.3
=================================
Bridges backend/ktrade_alpaca.py to the contract expected by ExecutionAgent
and KTradeCEO (see agent/ktrade_agent_v9.py).

Contract:
    get_account()  -> {"equity": float, "cash": float, "buying_power": float, ...}
    get_positions()-> list[dict]  (shape from fetch_positions())
    submit_bracket(ticker, qty, side, stop, target, client_order_id) -> order|None
    await_fill(order, timeout_s) -> {"filled_qty","filled_avg_price","status"}|None

SAFETY:
- submit_bracket is a NO-OP unless backend.ORDER_SUBMISSION_ENABLED is True
  (set KTRADE_PAPER_ORDER_SUBMISSION=true). It targets the PAPER endpoint only.
- await_fill polls real broker orders by client_order_id; it never fabricates a
  fill. If the order does not fill within the timeout it returns the last known
  (unfilled) status so the caller declines to record a fill.

Usage:
    from agent.broker_adapter import AlpacaBrokerAdapter
    from agent.ktrade_agent_v9 import KTradeCEO
    ceo = KTradeCEO(broker=AlpacaBrokerAdapter())
"""
from __future__ import annotations
import time
import logging

log = logging.getLogger("KTrade.broker_adapter")

try:
    from backend import ktrade_alpaca as alp
except Exception:  # pragma: no cover - allow import from inside backend/ on path
    import ktrade_alpaca as alp  # type: ignore


class AlpacaBrokerAdapter:
    def __init__(self, fill_timeout_s: float = 20.0, poll_interval_s: float = 1.0):
        self.fill_timeout_s = fill_timeout_s
        self.poll_interval_s = poll_interval_s

    # ---- truth ----------------------------------------------------------
    def get_account(self) -> dict:
        return alp.fetch_account() or {}

    def get_positions(self) -> list:
        return alp.fetch_positions() or []

    # ---- execution ------------------------------------------------------
    def submit_bracket(self, ticker, qty, side, stop, target, client_order_id=None):
        """Submit a paper bracket order. Returns the broker order dict, or None.

        Honors the global order-submission kill-flag. When disabled we log and
        return None so ExecutionAgent treats the trade as not filled (never a
        phantom position)."""
        if not getattr(alp, "ORDER_SUBMISSION_ENABLED", False):
            log.warning("submit_bracket BLOCKED: order submission disabled "
                        "(set KTRADE_PAPER_ORDER_SUBMISSION=true). %s %sx %s",
                        side, qty, ticker)
            return None
        if qty is None or float(qty) <= 0:
            log.warning("submit_bracket rejected: non-positive qty for %s", ticker)
            return None

        body = {
            "symbol":        str(ticker).upper(),
            "qty":           str(int(qty)),
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
            "order_class":   "bracket",
            "stop_loss":     {"stop_price":  str(round(float(stop), 2))},
            "take_profit":   {"limit_price": str(round(float(target), 2))},
        }
        order = alp.alpaca_post("/v2/orders", body)
        if not order:
            log.error("submit_bracket: broker returned no order for %s", ticker)
            return None
        # Carry the client_order_id we set so await_fill can match it.
        order.setdefault("client_order_id", client_order_id)
        log.info("submit_bracket OK %s %sx %s (coid=%s)", side, qty, ticker, client_order_id)
        return order

    def await_fill(self, order, timeout_s: float | None = None):
        """Poll the SPECIFIC submitted order by id until it fills/cancels or times
        out. v10.7: uses fetch_order(order_id) instead of scanning the latest-20
        list, and reports partial fills explicitly. Never fabricates a fill."""
        if not order:
            return None
        timeout_s = self.fill_timeout_s if timeout_s is None else timeout_s
        oid = order.get("id")
        coid = order.get("client_order_id")
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            raw = None
            try:
                raw = alp.fetch_order(oid) if oid else None
            except Exception:
                raw = None
            if not raw and coid:
                # fallback: match by client_order_id in recent orders
                for o in alp.fetch_orders():
                    if o.get("client_order_id") == coid:
                        raw = o
                        break
            if raw:
                last = raw
                status = str(raw.get("status") or "").lower()
                filled = float(raw.get("filled_qty") or 0)
                avg = float(raw.get("filled_avg_price") or raw.get("filled_avg") or 0)
                if status in {"filled", "partially_filled"} and filled > 0:
                    return {"filled_qty": filled, "filled_avg_price": avg,
                            "status": status, "partial": status == "partially_filled"}
                if status in {"canceled", "cancelled", "expired", "rejected"}:
                    return {"filled_qty": filled, "filled_avg_price": avg, "status": status}
            time.sleep(self.poll_interval_s)
        if last:
            return {"filled_qty": float(last.get("filled_qty") or 0),
                    "filled_avg_price": float(last.get("filled_avg_price") or last.get("filled_avg") or 0),
                    "status": str(last.get("status") or "timeout")}
        return {"filled_qty": 0, "filled_avg_price": 0.0, "status": "not_found"}

    # ---- v10.7: emergency actions used by EmergencyController ----
    def cancel_all_orders(self):
        return alp.cancel_all_orders()

    def close_all_positions(self):
        return alp.close_all_positions()


# Optional convenience: a market-state callable for KTradeCEO(market_fn=...).
def make_market_fn():
    """Returns a callable producing {'vix','spy_price'} from available feeds.
    VIX is left None unless a real source is wired (documented gap)."""
    def _fn():
        spy = 0.0
        try:
            prices = alp.fetch_prices(["SPY"]) or {}
            spy = float(prices.get("SPY", 0) or 0)
        except Exception:
            pass
        out = {}
        if spy > 0:
            out["spy_price"] = spy
        # out["vix"] = <wire a real VIX source here>  # e.g. ^VIX via data feed
        return out
    return _fn
