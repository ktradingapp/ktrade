"""
promotion_gate.py  (KTrade v11.1)
=================================
Paper -> live promotion gate.

A symbol must PROVE itself on paper before the agent is allowed to send it a
LIVE order. Adapted from the paper-first graduation pattern seen in a peer agent
and hardened to KTrade conventions.

Two responsibilities
--------------------
1. RECORD  - every verified closed *paper* trade updates a per-symbol ledger
             (trade count, wins, cumulative + average realised P&L, first-seen,
             last-trade time). Records are fed from the broker_reconciler's
             *truth* closed trades only (real fills, never synthetic marks), so
             the ledger inherits the same anti-contamination guarantee as the
             dashboard win-rate.
2. GATE    - ``block_reason_if_live(symbol)`` returns a reason string when a LIVE
             order for an un-graduated symbol should be blocked, else ``None``.

Safety properties (by design)
-----------------------------
* DISABLED BY DEFAULT (``KTRADE_PROMOTION_ENABLED=false``). Turning it on only
  starts building the ledger; it changes **no** paper behaviour.
* The gate can ONLY ever BLOCK a *live* order for an un-graduated name. It can
  never cause a trade that would not otherwise happen, and it never touches the
  paper path.
* SQLite (WAL) primary with atomic-JSON fallback, mirroring state_store.py /
  ktrade_db.py. Every public method is wrapped so a storage error degrades to
  "not promoted" (fail-safe: unproven names stay on paper) rather than raising
  into the order path.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from typing import Optional, Tuple

log = logging.getLogger("promotion_gate")

# ── Configuration (all overridable via environment) ──────────────────────────
def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def promotion_enabled() -> bool:
    return _b("KTRADE_PROMOTION_ENABLED", "false")


def live_mode() -> bool:
    """Live trading is on if EITHER the agent flag or a backend override is set.
    In paper mode the gate is a pure recorder and never blocks anything."""
    return _b("LIVE_TRADING", "false") or _b("KTRADE_LIVE_TRADING", "false")


def _cfg() -> dict:
    return {
        "min_trades":        int(os.getenv("KTRADE_PROMOTION_MIN_TRADES", "5")),
        "min_win_rate":      float(os.getenv("KTRADE_PROMOTION_MIN_WIN_RATE", "0.55")),
        "min_avg_pnl":       float(os.getenv("KTRADE_PROMOTION_MIN_AVG_PNL", "0.0")),
        "trial_days":        float(os.getenv("KTRADE_PROMOTION_TRIAL_DAYS", "5")),
        "require_net_pos":   _b("KTRADE_PROMOTION_REQUIRE_NET_POSITIVE", "true"),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


_DDL = """
CREATE TABLE IF NOT EXISTS promotion_ledger (
    symbol      TEXT PRIMARY KEY,
    trades      INTEGER NOT NULL DEFAULT 0,
    wins        INTEGER NOT NULL DEFAULT 0,
    gross_pnl   REAL    NOT NULL DEFAULT 0.0,
    first_seen  TEXT,
    last_trade  TEXT,
    promoted_at TEXT
)
"""

_DDL_SEEN = """
CREATE TABLE IF NOT EXISTS promotion_seen_trips (
    trip_id TEXT PRIMARY KEY,
    seen_at TEXT
)
"""


class PromotionGate:
    """Per-symbol paper track record + graduation check. Thread-safe."""

    def __init__(self, db_path: str = "data/ktrade.db",
                 json_path: str = "data/promotion_ledger.json"):
        self.db_path = db_path
        self.json_path = json_path
        self._lock = threading.RLock()
        # Mirror ktrade_db.py: SQLite unless the operator forced JSON state.
        self._sqlite = os.getenv("KTRADE_STATE_BACKEND", "sqlite").lower() == "sqlite"
        if self._sqlite:
            try:
                self._init_db()
            except Exception as exc:  # pragma: no cover - storage degradation
                log.warning("Promotion ledger SQLite init failed, JSON fallback: %s", exc)
                self._sqlite = False
        log.info("PromotionGate backend: %s | enabled=%s live=%s",
                 "SQLite" if self._sqlite else f"JSON ({self.json_path})",
                 promotion_enabled(), live_mode())

    # ── storage ──────────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._conn() as c:
            c.execute(_DDL)
            c.execute(_DDL_SEEN)

    def _json_load(self) -> dict:
        try:
            with open(self.json_path, "r") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _json_save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.json_path) or ".", exist_ok=True)
        d = os.path.dirname(self.json_path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.json_path)  # atomic
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    def _read(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if self._sqlite:
            with self._conn() as c:
                row = c.execute(
                    "SELECT * FROM promotion_ledger WHERE symbol=?", (symbol,)
                ).fetchone()
            return dict(row) if row else {}
        return self._json_load().get(symbol, {})

    def _write(self, symbol: str, rec: dict) -> None:
        symbol = symbol.upper()
        if self._sqlite:
            with self._conn() as c:
                c.execute(
                    """INSERT INTO promotion_ledger
                       (symbol, trades, wins, gross_pnl, first_seen, last_trade, promoted_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(symbol) DO UPDATE SET
                         trades=excluded.trades, wins=excluded.wins,
                         gross_pnl=excluded.gross_pnl, first_seen=excluded.first_seen,
                         last_trade=excluded.last_trade, promoted_at=excluded.promoted_at""",
                    (symbol, rec["trades"], rec["wins"], rec["gross_pnl"],
                     rec.get("first_seen"), rec.get("last_trade"), rec.get("promoted_at")),
                )
            return
        data = self._json_load()
        data[symbol] = rec
        self._json_save(data)

    # ── public API ───────────────────────────────────────────────────────────
    def record_closed_trade(self, symbol: str, pnl: float,
                            won: Optional[bool] = None,
                            closed_at: Optional[str] = None) -> None:
        """Fold one verified closed paper trade into the symbol's ledger.

        `pnl` is the realised dollar P&L of the round-trip (from the truth
        reconciler). `won` defaults to pnl > 0. Never raises into the caller."""
        if not symbol:
            return
        try:
            with self._lock:
                rec = self._read(symbol) or {}
                trades = int(rec.get("trades", 0)) + 1
                win = (pnl > 0) if won is None else bool(won)
                wins = int(rec.get("wins", 0)) + (1 if win else 0)
                gross = float(rec.get("gross_pnl", 0.0)) + float(pnl)
                first = rec.get("first_seen") or (closed_at or _iso())
                rec = {
                    "trades": trades, "wins": wins, "gross_pnl": round(gross, 4),
                    "first_seen": first, "last_trade": closed_at or _iso(),
                    "promoted_at": rec.get("promoted_at"),
                }
                # Stamp graduation once thresholds are first met (sticky).
                if not rec["promoted_at"]:
                    ok, _ = self._evaluate(rec)
                    if ok:
                        rec["promoted_at"] = _iso()
                        log.info("PROMOTED %s -> live-eligible (%d trades, %.0f%% win, $%.2f net)",
                                 symbol.upper(), trades, 100.0 * wins / trades, gross)
                self._write(symbol, rec)
        except Exception as exc:  # pragma: no cover
            log.warning("record_closed_trade(%s) failed (ignored): %s", symbol, exc)

    def _seen_trip(self, trip_id: str, mark: bool = False) -> bool:
        """Return True if this round-trip was already recorded. If mark=True and
        it was not seen, record it as seen and return False."""
        if self._sqlite:
            with self._conn() as c:
                row = c.execute(
                    "SELECT 1 FROM promotion_seen_trips WHERE trip_id=?", (trip_id,)
                ).fetchone()
                if row:
                    return True
                if mark:
                    c.execute(
                        "INSERT OR IGNORE INTO promotion_seen_trips(trip_id, seen_at) VALUES(?,?)",
                        (trip_id, _iso()),
                    )
                return False
        data = self._json_load()
        seen = data.setdefault("__seen_trips__", {})
        if trip_id in seen:
            return True
        if mark:
            seen[trip_id] = _iso()
            self._json_save(data)
        return False

    @staticmethod
    def _trip_id(trip: dict) -> str:
        """Stable identity for a FIFO round-trip so the poll-driven reconciler
        can be called repeatedly without double-counting the same trade."""
        return "|".join(str(trip.get(k, "")) for k in
                        ("ticker", "exit_time", "entry_time", "qty", "pnl"))

    def record_truth_trades(self, trades: list) -> int:
        """Idempotently fold a list of truth-reconciled closed trades (each the
        dict from RoundTrip.to_dict()) into the ledger. Returns how many NEW
        trades were recorded. Never raises into the caller."""
        n = 0
        for t in (trades or []):
            try:
                tid = self._trip_id(t)
                with self._lock:
                    if self._seen_trip(tid, mark=True):
                        continue
                self.record_closed_trade(
                    t.get("ticker") or t.get("symbol"),
                    float(t.get("pnl") or 0.0),
                    closed_at=t.get("exit_time"),
                )
                n += 1
            except Exception as exc:  # pragma: no cover
                log.warning("record_truth_trades skipped one (%s)", exc)
        return n

    def _evaluate(self, rec: dict) -> Tuple[bool, str]:
        """Pure threshold check against a ledger record. Returns (eligible, why-not)."""
        cfg = _cfg()
        trades = int(rec.get("trades", 0))
        if trades < cfg["min_trades"]:
            return False, f"{trades}/{cfg['min_trades']} paper trades"
        wins = int(rec.get("wins", 0))
        win_rate = wins / trades if trades else 0.0
        if win_rate < cfg["min_win_rate"]:
            return False, f"win rate {win_rate:.0%} < {cfg['min_win_rate']:.0%}"
        gross = float(rec.get("gross_pnl", 0.0))
        if cfg["require_net_pos"] and gross <= 0:
            return False, f"net P&L ${gross:.2f} not positive"
        avg = gross / trades if trades else 0.0
        if avg < cfg["min_avg_pnl"]:
            return False, f"avg P&L ${avg:.2f} < ${cfg['min_avg_pnl']:.2f}"
        first = _parse_iso(rec.get("first_seen"))
        if first is not None:
            age_days = (_now() - first).total_seconds() / 86400.0
            if age_days < cfg["trial_days"]:
                return False, f"trial age {age_days:.1f}d < {cfg['trial_days']}d"
        return True, "graduated"

    def is_promoted(self, symbol: str) -> Tuple[bool, str]:
        """True only once `symbol` has graduated. Fail-safe: any error -> not
        promoted (the symbol stays on paper)."""
        try:
            rec = self._read(symbol)
            if not rec:
                return False, "no paper history"
            if rec.get("promoted_at"):
                return True, "graduated"
            ok, why = self._evaluate(rec)
            if ok:
                # Graduation is durable: stamp it the moment it is first observed,
                # whether that is during a trade record or a live-order gate check.
                with self._lock:
                    rec["promoted_at"] = _iso()
                    self._write(symbol, rec)
                    log.info("PROMOTED %s -> live-eligible", symbol.upper())
            return ok, why
        except Exception as exc:  # pragma: no cover
            log.warning("is_promoted(%s) failed -> treating as NOT promoted: %s", symbol, exc)
            return False, "ledger error (fail-safe)"

    def block_reason_if_live(self, symbol: str) -> Optional[str]:
        """The only method the order path needs.

        Returns a human reason to BLOCK a live order, or None to allow.
        Allows everything unless: promotion is enabled AND we are in live mode
        AND the symbol has not graduated. Paper orders are never blocked."""
        if not promotion_enabled() or not live_mode():
            return None
        ok, why = self.is_promoted(symbol)
        if ok:
            return None
        return f"promotion gate: {symbol.upper()} not graduated to live ({why})"

    def status(self, symbol: str) -> dict:
        rec = self._read(symbol)
        ok, why = self.is_promoted(symbol)
        trades = int(rec.get("trades", 0)) if rec else 0
        wins = int(rec.get("wins", 0)) if rec else 0
        return {
            "symbol": symbol.upper(),
            "trades": trades,
            "win_rate": round(100.0 * wins / trades, 1) if trades else 0.0,
            "net_pnl": round(float(rec.get("gross_pnl", 0.0)), 2) if rec else 0.0,
            "promoted": ok,
            "reason": why,
            "first_seen": rec.get("first_seen") if rec else None,
        }

    def all_status(self) -> list:
        if self._sqlite:
            with self._conn() as c:
                syms = [r["symbol"] for r in
                        c.execute("SELECT symbol FROM promotion_ledger ORDER BY symbol").fetchall()]
        else:
            syms = sorted(self._json_load().keys())
        return [self.status(s) for s in syms]

    def reset(self, symbol: Optional[str] = None) -> None:
        with self._lock:
            if self._sqlite:
                with self._conn() as c:
                    if symbol:
                        c.execute("DELETE FROM promotion_ledger WHERE symbol=?", (symbol.upper(),))
                    else:
                        c.execute("DELETE FROM promotion_ledger")
            else:
                data = {} if symbol is None else self._json_load()
                if symbol and data:
                    data.pop(symbol.upper(), None)
                self._json_save(data)


# ── module-level singleton + thin convenience wrappers ───────────────────────
_GATE: Optional[PromotionGate] = None
_GATE_LOCK = threading.Lock()


def get_gate() -> PromotionGate:
    global _GATE
    if _GATE is None:
        with _GATE_LOCK:
            if _GATE is None:
                _GATE = PromotionGate()
    return _GATE


def record_closed_trade(symbol: str, pnl: float, won: Optional[bool] = None,
                        closed_at: Optional[str] = None) -> None:
    if promotion_enabled():
        get_gate().record_closed_trade(symbol, pnl, won=won, closed_at=closed_at)


def block_reason_if_live(symbol: str) -> Optional[str]:
    try:
        return get_gate().block_reason_if_live(symbol)
    except Exception:  # pragma: no cover - never break the order path
        return None


def record_truth_trades(trades: list) -> int:
    """Idempotently sync truth-reconciled closed trades into the ledger.
    No-op (returns 0) unless the promotion gate is enabled."""
    if not promotion_enabled():
        return 0
    try:
        return get_gate().record_truth_trades(trades)
    except Exception:  # pragma: no cover
        return 0
