"""
position_fills.py - KTrade v10.7
================================
Correct partial-fill accounting (#9). A SELL reduces quantity (weighted average
cost preserved) instead of popping the whole long, and a BUY blends average cost.
"""
from __future__ import annotations


def apply_fill_to_position(open_positions: dict, ticker: str, side: str,
                           qty: float, price: float) -> dict:
    """Mutate and return open_positions for a (possibly partial) fill.

    open_positions: {TICKER: {"qty": float, "avg_cost": float}}
    """
    ticker = (ticker or "").upper()
    qty = float(qty)
    price = float(price)
    if qty <= 0 or price <= 0:
        raise ValueError("fill qty and price must be positive")

    existing = open_positions.get(ticker, {"qty": 0.0, "avg_cost": price})
    old_qty = float(existing.get("qty", 0.0))
    old_cost = float(existing.get("avg_cost", price))

    side = (side or "").lower()
    if side == "buy":
        new_qty = old_qty + qty
        new_avg = ((old_qty * old_cost) + (qty * price)) / new_qty if new_qty else price
        open_positions[ticker] = {"qty": new_qty, "avg_cost": new_avg}
    elif side == "sell":
        new_qty = old_qty - qty
        if new_qty > 1e-9:
            open_positions[ticker] = {"qty": new_qty, "avg_cost": old_cost}
        else:
            open_positions.pop(ticker, None)
    else:
        raise ValueError(f"Unknown side: {side}")
    return open_positions
