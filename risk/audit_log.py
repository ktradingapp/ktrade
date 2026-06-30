"""
audit_log.py  (KTrade v11.3 — Milestone 1)
==========================================
Immutable, tamper-evident decision audit trail.

Every auto-trade decision (submitted, blocked, cycle-halt) is appended as one
JSON line to an append-only log, hash-chained so that any later edit or deletion
of a record breaks verification. This is the post-mortem / compliance spine: for
any fill or block you can reconstruct exactly which inputs (conviction, regime
profile, risk-gate result, portfolio context) produced the decision.

Record shape:
    {"seq":N,"ts":"...","event":"submitted","payload":{...},
     "prev":"<hash of N-1>","hash":"<sha256(prev + canonical(seq,ts,event,payload))>"}

`verify()` re-walks the chain and reports the first broken link.

Always-on by default (KTRADE_AUDIT_ENABLED=true) — it is pure logging and never
changes trading behaviour. Defensive: a logging failure is swallowed, never
raised into the order path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

log = logging.getLogger("audit_log")

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_GENESIS = "0" * 64


def enabled() -> bool:
    return os.getenv("KTRADE_AUDIT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _path() -> Path:
    return Path(os.getenv("KTRADE_AUDIT_PATH", str(_PROJECT_DIR / "logs" / "ktrade_audit.jsonl")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(prev: str, seq: int, ts: str, event: str, payload: dict) -> str:
    body = json.dumps({"seq": seq, "ts": ts, "event": event, "payload": payload},
                      separators=(",", ":"), sort_keys=True)
    return hashlib.sha256((prev + body).encode()).hexdigest()


class AuditLog:
    def __init__(self):
        self._lock = threading.RLock()

    def _tail(self) -> Tuple[int, str]:
        """Return (last_seq, last_hash) by reading the final line. (-1, GENESIS)
        if the log is empty/missing."""
        p = _path()
        if not p.exists():
            return -1, _GENESIS
        last = None
        try:
            with open(p, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last = line
        except Exception:
            return -1, _GENESIS
        if not last:
            return -1, _GENESIS
        try:
            rec = json.loads(last)
            return int(rec["seq"]), str(rec["hash"])
        except Exception:
            return -1, _GENESIS

    def record(self, event: str, payload: dict) -> None:
        """Append one chained record. Never raises into the caller."""
        if not enabled():
            return
        try:
            with self._lock:
                last_seq, last_hash = self._tail()
                seq = last_seq + 1
                ts = _now()
                h = _hash(last_hash, seq, ts, event, payload)
                rec = {"seq": seq, "ts": ts, "event": event, "payload": payload,
                       "prev": last_hash, "hash": h}
                p = _path()
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a") as fh:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except Exception as exc:  # pragma: no cover
            log.warning("audit record failed (ignored): %s", exc)

    def verify(self) -> Tuple[bool, str]:
        """Re-walk the chain. Returns (ok, message). A broken link names the seq."""
        p = _path()
        if not p.exists():
            return True, "empty log"
        prev = _GENESIS
        expect_seq = 0
        try:
            with open(p, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if int(rec["seq"]) != expect_seq:
                        return False, f"seq gap at {rec['seq']} (expected {expect_seq})"
                    if rec["prev"] != prev:
                        return False, f"broken prev-link at seq {rec['seq']}"
                    h = _hash(prev, rec["seq"], rec["ts"], rec["event"], rec["payload"])
                    if h != rec["hash"]:
                        return False, f"hash mismatch at seq {rec['seq']} (record altered)"
                    prev = rec["hash"]
                    expect_seq += 1
        except Exception as exc:
            return False, f"verify error: {exc}"
        return True, f"chain intact ({expect_seq} records)"


_LOG = None
_LOG_LOCK = threading.Lock()


def get_audit() -> AuditLog:
    global _LOG
    if _LOG is None:
        with _LOG_LOCK:
            if _LOG is None:
                _LOG = AuditLog()
    return _LOG


def record(event: str, payload: dict) -> None:
    try:
        get_audit().record(event, payload)
    except Exception:  # pragma: no cover
        pass
