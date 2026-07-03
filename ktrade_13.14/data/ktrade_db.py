"""
ktrade_db.py - KTrade v10.8  (PILLAR 1: Transactional Storage Coordinator)
==========================================================================
Replaces volatile JSON state files with a transactional SQLite store, safe for
cross-thread (and cross-process: agent + Flask backend) access.

Edge-case hardening over the original draft:
  * Connections are CLOSED after every operation (the `with conn:` context
    manager commits but does NOT close — that leaked a handle per call).
  * WAL journal mode + busy_timeout for concurrent agent/backend writers.
  * Upsert for risk_state; append-only ledger for emergency_state (audit trail).
  * Every method is wrapped so a DB failure degrades gracefully instead of
    crashing the trading loop.
"""
from __future__ import annotations
import sqlite3
import json
import logging
from contextlib import closing
from pathlib import Path
from threading import RLock
from datetime import datetime

log = logging.getLogger("KTrade.db")


class KTradeSQLiteStore:
    def __init__(self, db_path: str = "data/ktrade.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self._init_db()

    def _get_connection(self):
        """New connection with a robust busy timeout + WAL for concurrency."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        return conn

    def _init_db(self):
        with self.lock, closing(self._get_connection()) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS risk_state (
                    key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS emergency_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    active INTEGER NOT NULL,
                    reason TEXT,
                    triggered_at TEXT NOT NULL
                )""")
            conn.commit()

    # ---- risk state (upsert) ----
    def save_risk_state(self, key: str, state_dict: dict) -> bool:
        try:
            with self.lock, closing(self._get_connection()) as conn:
                conn.execute("""
                    INSERT INTO risk_state (key, state_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        state_json=excluded.state_json,
                        updated_at=excluded.updated_at
                """, (key, json.dumps(state_dict, default=str), datetime.utcnow().isoformat()))
                conn.commit()
            return True
        except Exception as exc:
            log.error("save_risk_state(%s) failed: %s", key, exc)
            return False

    def load_risk_state(self, key: str) -> dict:
        try:
            with self.lock, closing(self._get_connection()) as conn:
                row = conn.execute(
                    "SELECT state_json FROM risk_state WHERE key = ?", (key,)).fetchone()
            if row:
                return json.loads(row[0])
        except Exception as exc:
            log.error("load_risk_state(%s) failed: %s", key, exc)
        return {}

    # ---- emergency ledger (append-only) ----
    def save_emergency_state(self, active: bool, reason: str, triggered_at: str) -> bool:
        try:
            with self.lock, closing(self._get_connection()) as conn:
                conn.execute("""
                    INSERT INTO emergency_state (active, reason, triggered_at)
                    VALUES (?, ?, ?)
                """, (1 if active else 0, reason, triggered_at))
                conn.commit()
            return True
        except Exception as exc:
            log.error("save_emergency_state failed: %s", exc)
            return False

    def load_latest_emergency_state(self) -> dict:
        try:
            with self.lock, closing(self._get_connection()) as conn:
                row = conn.execute("""
                    SELECT active, reason, triggered_at FROM emergency_state
                    ORDER BY id DESC LIMIT 1
                """).fetchone()
            if row:
                return {"active": bool(row[0]), "reason": row[1], "triggered_at": row[2]}
        except Exception as exc:
            log.error("load_latest_emergency_state failed: %s", exc)
        return {"active": False, "reason": "", "triggered_at": ""}


# Module-level singleton + flag so state_store / emergency share one DB and can
# fall back to JSON if SQLite is disabled.
import os as _os
USE_SQLITE = _os.getenv("KTRADE_STATE_BACKEND", "sqlite").lower() == "sqlite"
_STORE = None

def _default_db_path() -> str:
    explicit = _os.getenv("KTRADE_DB_PATH", "").strip()
    if explicit:
        return explicit
    try:
        from ktrade_runtime.paths import db_path as _runtime_db_path
        return str(_runtime_db_path())
    except Exception:
        return "data/ktrade.db"

def get_store(db_path: str | None = None):
    global _STORE
    if _STORE is None:
        _STORE = KTradeSQLiteStore(db_path or _default_db_path())
    return _STORE
