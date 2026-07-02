"""
broker_reconciler.py — KTrade v10.1 broker-truth reconciliation
================================================================
Builds closed trades from REAL Alpaca fill activities (never last-quote marks)
and detects position desyncs between an intended book and broker truth.

This is the fix for the "broker flat reconcile" problem: closed-trade P&L is
computed only from actual fills, so there is no "exclude from performance"
category and the dashboard's win-rate / profit-factor become trustworthy.

Transport-agnostic: pass a `get_json(path) -> dict|list|None` callable so this
reuses the backend's existing `alpaca_get` (same headers, error logging, creds).
"""

from __future__ import annotations
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class RoundTrip:
    symbol: str
    qty: float
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str
    realized_pnl: float
    side: str = "long"
    verified: bool = True   # always built from real fills

    def to_dict(self) -> dict:
        return {
            "ticker": self.symbol, "side": "BUY" if self.side == "long" else "SELL",
            "qty": self.qty, "entry": round(self.entry_price, 2),
            "exit": round(self.exit_price, 2), "entry_time": self.entry_time,
            "exit_time": self.exit_time, "pnl": round(self.realized_pnl, 2),
            "verified": self.verified, "reason": "real fill (verified)",
        }


@dataclass
class DesyncEvent:
    symbol: str
    agent_qty: float
    broker_qty: float
    reason: str

    def to_dict(self) -> dict:
        return {"ticker": self.symbol, "agent_qty": self.agent_qty,
                "broker_qty": self.broker_qty, "reason": self.reason}


@dataclass
class ReconcileResult:
    round_trips: list = field(default_factory=list)
    desyncs: list = field(default_factory=list)
    realized_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0

    @property
    def desync_count(self) -> int:
        return len(self.desyncs)

    @property
    def profit_factor(self) -> Optional[float]:
        return round(self.gross_win / self.gross_loss, 3) if self.gross_loss else None

    def stats(self) -> dict:
        trips = len(self.round_trips)
        return {
            "trips": trips, "wins": self.wins, "losses": self.losses,
            "win_rate": round(100.0 * self.wins / trips, 2) if trips else 0.0,
            "realized_pnl": round(self.realized_pnl, 2),
            "profit_factor": self.profit_factor,
            "avg_win": round(self.gross_win / self.wins, 2) if self.wins else 0.0,
            "avg_loss": round(-self.gross_loss / self.losses, 2) if self.losses else 0.0,
            "verified": True, "source": "alpaca fill activities",
        }


class BrokerReconciler:
    def __init__(self, get_json: Callable[[str], object],
                 max_desyncs_before_halt: int = 3):
        """get_json(path) should behave like the backend's alpaca_get:
        return parsed JSON, or None on failure."""
        self._get = get_json
        self.max_desyncs_before_halt = max_desyncs_before_halt

    # --- data --------------------------------------------------------------
    def _fills(self, after_iso: Optional[str] = None) -> list:
        out, token, page = [], None, 0
        while page < 50:  # safety cap on pagination
            path = "/v2/account/activities/FILL?page_size=100"
            if after_iso:
                path += f"&after={after_iso}"
            if token:
                path += f"&page_token={token}"
            batch = self._get(path)
            if not batch:
                break
            out.extend(batch)
            token = batch[-1].get("id")
            page += 1
            if len(batch) < 100:
                break
        return out

    def _broker_positions(self) -> dict:
        data = self._get("/v2/positions")
        result = {}
        for p in data or []:
            sym = (p.get("symbol") or "").upper()
            if sym:
                result[sym] = float(p.get("qty", 0) or 0)
        return result

    # --- core --------------------------------------------------------------
    def reconcile(self, agent_open_book: Optional[dict] = None,
                  after_iso: Optional[str] = None) -> ReconcileResult:
        result = ReconcileResult()
        fills = self._fills(after_iso)
        fills.sort(key=lambda f: f.get("transaction_time", ""))

        lots: dict = defaultdict(deque)  # symbol -> FIFO of open buy lots
        for f in fills:
            sym = (f.get("symbol") or "").upper()
            try:
                qty = abs(float(f.get("qty", 0)))
                price = float(f.get("price", 0))
            except (TypeError, ValueError):
                continue
            t = f.get("transaction_time", "")
            side = (f.get("side") or "").lower()
            if qty <= 0 or not sym:
                continue
            if side == "buy":
                lots[sym].append([qty, price, t])
            elif side == "sell":
                self._close(sym, qty, price, t, lots, result)

        # Desync: intended book says open but broker shows flat/different.
        if agent_open_book:
            broker = self._broker_positions()
            for sym, want in agent_open_book.items():
                sym = sym.upper()
                if abs(float(want)) < 1e-9:
                    continue
                have = broker.get(sym, 0.0)
                if abs(have) < 1e-9:
                    result.desyncs.append(DesyncEvent(
                        sym, float(want), 0.0,
                        "intended book open but broker flat — no exit fill found"))
                elif abs(have - float(want)) > 1e-6:
                    result.desyncs.append(DesyncEvent(
                        sym, float(want), have,
                        "qty mismatch between intended book and broker"))
        return result

    def _close(self, sym, sell_qty, sell_price, sell_time, lots, result):
        remaining = sell_qty
        while remaining > 1e-9 and lots[sym]:
            lot_qty, lot_price, lot_time = lots[sym][0]
            matched = min(remaining, lot_qty)
            pnl = (sell_price - lot_price) * matched
            result.round_trips.append(RoundTrip(
                symbol=sym, qty=matched, entry_price=lot_price,
                exit_price=sell_price, entry_time=lot_time, exit_time=sell_time,
                realized_pnl=pnl, side="long"))
            result.realized_pnl += pnl
            if pnl >= 0:
                result.wins += 1
                result.gross_win += pnl
            else:
                result.losses += 1
                result.gross_loss += -pnl
            lot_qty -= matched
            remaining -= matched
            if lot_qty <= 1e-9:
                lots[sym].popleft()
            else:
                lots[sym][0][0] = lot_qty

    # --- circuit breaker ---------------------------------------------------
    def should_halt(self, result: ReconcileResult) -> bool:
        return result.desync_count >= self.max_desyncs_before_halt
