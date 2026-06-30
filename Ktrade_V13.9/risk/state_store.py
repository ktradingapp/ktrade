"""
state_store.py - KTrade v10.8
=============================
Risk-state persistence. v10.8: backed by transactional SQLite (PILLAR 1) when
KTRADE_STATE_BACKEND=sqlite (default); falls back to atomic JSON writes
otherwise. Public API (load/save) is unchanged for backward compatibility.
"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime
from threading import RLock
import logging

log = logging.getLogger("KTrade.state_store")

try:
    from data.ktrade_db import get_store, USE_SQLITE
except Exception:
    try:
        from ktrade_db import get_store, USE_SQLITE
    except Exception:
        get_store, USE_SQLITE = None, False


class RiskStateStore:
    KEY = "risk"

    def __init__(self, path="data/risk_state.json"):
        self.path = Path(path)
        self.lock = RLock()
        self._sqlite = bool(USE_SQLITE and get_store is not None)
        log.info("RiskStateStore backend: %s", "SQLite" if self._sqlite else f"JSON ({self.path})")

    def load(self) -> dict:
        with self.lock:
            if self._sqlite:
                try:
                    return get_store().load_risk_state(self.KEY)
                except Exception as exc:
                    log.warning("SQLite load failed, JSON fallback: %s", exc)
            if not self.path.exists():
                return {}
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Could not load risk state: %s", exc)
                return {}

    def save(self, state: dict) -> None:
        with self.lock:
            state = dict(state)
            state["saved_at"] = datetime.utcnow().isoformat()
            if self._sqlite:
                try:
                    if get_store().save_risk_state(self.KEY, state):
                        return
                except Exception as exc:
                    log.warning("SQLite save failed, JSON fallback: %s", exc)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
                tmp.replace(self.path)
            except Exception as exc:
                log.error("Could not save risk state: %s", exc)
