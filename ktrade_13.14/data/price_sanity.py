"""
price_sanity.py — KTrade v10.2 bad-tick / decimal-shift guard
=============================================================
Motivation: a peer momentum agent ingested a 10x-corrupted quote for KLAC
($2300.52 vs a real ~$236), entered on it, set stop/target 10x off, and booked a
phantom -89.7% loss. A momentum strategy is *especially* exposed: a 10x spike looks
like the strongest possible momentum, so a bad tick doesn't slip past the signal —
it triggers it.

This guard validates every live price against a per-symbol reference and rejects:
  - decimal-shift errors (price/ref ~ a power of 10) — the exact KLAC failure
  - implausible single-tick jumps beyond a configurable percent
  - non-positive prices

Use `scrub()` at quote ingestion (carry-forward last-good on rejection) and
`validate_entry()` as a hard gate before placing an order.
"""

from __future__ import annotations
import math
import logging
from datetime import datetime

log = logging.getLogger("KTrade.price_sanity")


class PriceSanityGuard:
    def __init__(self, max_jump_pct: float = 40.0,
                 decimal_shift_tol: float = 0.06,
                 max_flags: int = 200):
        # max_jump_pct: reject a tick that moves more than this vs reference.
        #   Tune per universe; the decimal-shift check below is independent of it.
        # decimal_shift_tol: how close (in log10 units) price/ref must be to a
        #   power of 10 to be called a decimal error (0.06 ~ within ~15%).
        self.max_jump_pct = max_jump_pct
        self.decimal_shift_tol = decimal_shift_tol
        self.last_good: dict[str, float] = {}
        self.flags: list[dict] = []
        self._max_flags = max_flags

    def seed_reference(self, symbol: str, price: float) -> None:
        """Optionally bootstrap a reference from a trusted source (e.g. prior close)
        so the FIRST live tick can also be validated."""
        if price and price > 0:
            self.last_good[symbol.upper()] = float(price)

    def check(self, symbol: str, price: float,
              reference: float | None = None) -> tuple[bool, str]:
        sym = symbol.upper()
        try:
            price = float(price)
        except (TypeError, ValueError):
            return False, "nonnumeric_price"
        if price <= 0:
            return False, "nonpositive_price"

        ref = reference if reference is not None else self.last_good.get(sym)
        if not ref or ref <= 0:
            return True, "no_reference"   # cannot judge yet; accept and learn

        ratio = price / ref
        logr = math.log10(ratio)
        k = round(logr)
        if k != 0 and abs(logr - k) <= self.decimal_shift_tol:
            factor = f"x{10**k}" if k > 0 else f"/{10**(-k)}"
            return False, f"decimal_shift_{factor}"

        jump = abs(price - ref) / ref * 100.0
        if jump > self.max_jump_pct:
            return False, f"jump_{jump:.0f}pct"
        return True, "ok"

    def scrub(self, prices: dict, references: dict | None = None) -> dict:
        """Validate a snapshot dict {symbol: price}. Accepted prices update the
        reference; rejected ones carry forward last-good (or drop if none)."""
        out = {}
        for sym, px in prices.items():
            ref = (references or {}).get(sym)
            ok, reason = self.check(sym, px, ref)
            if ok:
                self.last_good[sym.upper()] = float(px)
                out[sym] = px
            else:
                self._flag(sym, px, reason)
                if sym.upper() in self.last_good:
                    out[sym] = self.last_good[sym.upper()]   # carry forward
                # else: drop — no safe value to substitute
        return out

    def validate_entry(self, symbol: str, price: float,
                       reference: float | None = None) -> dict:
        """Hard gate before placing an order. ok=False means DO NOT TRADE."""
        ok, reason = self.check(symbol, price, reference)
        if not ok:
            self._flag(symbol, price, reason)
        return {"ok": ok, "reason": reason,
                "symbol": symbol.upper(), "price": price,
                "reference": reference if reference is not None
                else self.last_good.get(symbol.upper())}

    def _flag(self, symbol: str, price, reason: str) -> None:
        entry = {"time": datetime.now().isoformat(), "symbol": symbol.upper(),
                 "price": price, "reason": reason,
                 "reference": self.last_good.get(symbol.upper())}
        log.warning("BAD TICK rejected %s @ %s (%s, ref=%s)",
                    symbol, price, reason, entry["reference"])
        self.flags.append(entry)
        if len(self.flags) > self._max_flags:
            self.flags = self.flags[-self._max_flags:]


# Module-level singleton shared across the data layer.
PRICE_GUARD = PriceSanityGuard()
