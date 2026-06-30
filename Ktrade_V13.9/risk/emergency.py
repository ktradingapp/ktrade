"""
emergency.py - KTrade v10.8
===========================
Persistent emergency controller (kill switch + cancel/flatten). v10.8: state is
persisted to the transactional SQLite ledger (PILLAR 1) when
KTRADE_STATE_BACKEND=sqlite (default), giving an append-only audit trail of every
kill/reset; falls back to a JSON file otherwise. Public API unchanged.

broker (optional) must expose: cancel_all_orders(), close_all_positions().
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
from threading import Event, RLock

log = logging.getLogger("KTrade.emergency")

try:
    from data.ktrade_db import get_store, USE_SQLITE
except Exception:
    try:
        from ktrade_db import get_store, USE_SQLITE
    except Exception:
        get_store, USE_SQLITE = None, False


@dataclass
class KillEvent:
    active: bool
    reason: str
    triggered_at: str


class EmergencyController:
    def __init__(self, broker=None, state_file: str = "data/kill_switch.json"):
        self.broker = broker
        self.state_file = Path(state_file)
        self._event = Event()
        self._lock = RLock()
        self.reason = ""
        self.triggered_at = ""
        # v12.6: only use the shared SQLite store for the DEFAULT state_file. When a
        # caller passes an explicit state_file (e.g. tests, or any isolated instance),
        # honor that file instead of the global store so instances don't share state.
        _is_default_state = str(state_file) == "data/kill_switch.json"
        self._sqlite = bool(USE_SQLITE and get_store is not None and _is_default_state)
        # Restore persisted kill state on startup
        data = self._read_persisted()
        if data.get("active"):
            self._event.set()
            self.reason = data.get("reason", "persisted kill switch")
            self.triggered_at = data.get("triggered_at", "")
            log.critical("Restored persisted KILL state: %s", self.reason)

    # ---- persistence backends ----
    def _read_persisted(self) -> dict:
        if self._sqlite:
            try:
                return get_store().load_latest_emergency_state()
            except Exception as exc:
                log.warning("SQLite emergency load failed, JSON fallback: %s", exc)
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Could not read kill state: %s", exc)
        return {"active": False, "reason": "", "triggered_at": ""}

    def _write_persisted(self, active: bool, reason: str):
        self.triggered_at = datetime.utcnow().isoformat()
        if self._sqlite:
            try:
                if get_store().save_emergency_state(active, reason, self.triggered_at):
                    return
            except Exception as exc:
                log.warning("SQLite emergency save failed, JSON fallback: %s", exc)
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(
                {"active": active, "reason": reason, "triggered_at": self.triggered_at},
                indent=2), encoding="utf-8")
        except Exception as exc:
            log.error("Could not persist kill state: %s", exc)

    # ---- API ----
    def active(self) -> bool:
        return self._event.is_set()

    def status(self) -> dict:
        return {"active": self.active(), "reason": self.reason, "triggered_at": self.triggered_at}

    def trigger(self, reason: str, flatten: bool = False) -> None:
        with self._lock:
            already_active = self._event.is_set()
            if not already_active:
                self.reason = reason
                self._event.set()
                log.critical("KILL SWITCH: %s (flatten=%s)", reason, flatten)
                self._write_persisted(True, reason)
            else:
                log.critical(
                    "KILL SWITCH already active (%s); new request reason=%s flatten=%s",
                    self.reason, reason, flatten,
                )
            # v12.5: broker actions run on EVERY call, so a later flatten request is
            # honored even if the kill switch was already active.
            if self.broker is not None:
                try:
                    self.broker.cancel_all_orders()
                    log.critical("All open broker orders cancelled")
                except Exception as exc:
                    log.error("cancel_all_orders failed: %s", exc)
                if flatten:
                    try:
                        self.broker.close_all_positions()
                        log.critical("All positions flattened")
                    except Exception as exc:
                        log.error("close_all_positions failed: %s", exc)

    def reset(self) -> None:
        with self._lock:
            self.reason = ""
            self._event.clear()
            self._write_persisted(False, "")
            log.warning("Kill switch manually reset")
