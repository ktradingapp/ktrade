"""
manual_order_schema.py - KTrade v10.7
=====================================
Strict validation for dashboard /buy and /sell input (#1). Rejects malformed
tickers, non-positive qty, and limit orders missing a limit price BEFORE any
order reaches the broker.
"""
from __future__ import annotations
from typing import Literal, Optional
import re

try:
    from pydantic import BaseModel, Field, field_validator
    _HAVE_PYDANTIC = True
except Exception:
    _HAVE_PYDANTIC = False


_TICKER_RE = re.compile(r"[A-Z0-9.\-]{1,12}")


if _HAVE_PYDANTIC:
    class ManualOrderRequest(BaseModel):
        ticker: str = Field(..., min_length=1, max_length=12)
        side: Literal["buy", "sell"]
        qty: float = Field(..., gt=0)
        order_type: Literal["market", "limit"] = "market"
        limit_price: Optional[float] = Field(default=None, gt=0)

        @field_validator("ticker")
        @classmethod
        def clean_ticker(cls, value: str) -> str:
            ticker = value.strip().upper()
            if not _TICKER_RE.fullmatch(ticker):
                raise ValueError(f"Invalid ticker: {value}")
            return ticker

        @field_validator("limit_price")
        @classmethod
        def require_limit_price(cls, value, info):
            if info.data.get("order_type") == "limit" and value is None:
                raise ValueError("limit_price is required for limit orders")
            return value


def parse_manual_order(body: dict):
    """Return (parsed_dict, error_str). One of them is None."""
    if not isinstance(body, dict):
        return None, "no JSON body"
    side = str(body.get("side", "buy")).lower()
    payload = {
        "ticker": str(body.get("ticker", "")),
        "side": side,
        "qty": body.get("qty", 0),
        "order_type": str(body.get("type", body.get("order_type", "market"))).lower(),
        "limit_price": body.get("limit_price"),
    }
    if _HAVE_PYDANTIC:
        try:
            return ManualOrderRequest(**payload).model_dump(), None
        except Exception as exc:
            return None, str(exc)
    # fallback manual validation
    t = payload["ticker"].strip().upper()
    if not _TICKER_RE.fullmatch(t):
        return None, f"Invalid ticker: {payload['ticker']}"
    if payload["side"] not in ("buy", "sell"):
        return None, "side must be buy or sell"
    try:
        q = float(payload["qty"])
        if q <= 0:
            return None, "qty must be > 0"
    except Exception:
        return None, "qty must be numeric"
    if payload["order_type"] == "limit" and not payload["limit_price"]:
        return None, "limit_price required for limit orders"
    payload["ticker"] = t
    payload["qty"] = q
    return payload, None
