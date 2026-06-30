"""
earnings_calendar.py - KTrade v10.9
===================================
Earnings-event awareness (closes the gap vs peer agents that avoid/trim ahead of
earnings, e.g. MU). Provides the next earnings date per ticker so the RiskEngine
can (a) BLOCK new buys inside an earnings blackout window and (b) optionally EXIT
holdings before a binary earnings event.

Design:
  * Pluggable `fetch_fn(ticker) -> 'YYYY-MM-DD' | None` (default: Finnhub
    /calendar/earnings, free-tier friendly). Tests inject a fake.
  * On-disk cache (data/earnings_cache.json) with a TTL so we don't hammer the
    API; in-memory cache on top.
  * Fails OPEN as a no-op: if there's no key / no data / an error, it returns
    None and the gate simply doesn't fire (never blocks trading on a data outage).
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from pathlib import Path
import json
import logging
import os

log = logging.getLogger("KTrade.earnings")

_WARNED_NO_SOURCE = False


def _finnhub_fetch(ticker: str):
    """Return the nearest FUTURE earnings date 'YYYY-MM-DD' from Finnhub, or None.
    Free-tier friendly (/calendar/earnings). Never raises."""
    global _WARNED_NO_SOURCE
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        if not _WARNED_NO_SOURCE:
            _WARNED_NO_SOURCE = True
            log.warning("Earnings calendar inactive: FINNHUB_API_KEY not set "
                        "(earnings blackout/exit gates will be no-ops).")
        return None
    try:
        import requests
        today = date.today()
        to = today + timedelta(days=120)
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": today.isoformat(), "to": to.isoformat(),
                    "symbol": ticker.upper(), "token": key},
            timeout=15,
        )
        r.raise_for_status()
        rows = (r.json() or {}).get("earningsCalendar", []) or []
        future = sorted(d["date"] for d in rows if d.get("date") and d["date"] >= today.isoformat())
        return future[0] if future else None
    except Exception as exc:
        log.debug("Finnhub earnings fetch failed for %s: %s", ticker, exc)
        return None


class EarningsCalendar:
    def __init__(self, fetch_fn=None, cache_path="data/earnings_cache.json", ttl_hours=24):
        self.fetch_fn = fetch_fn or _finnhub_fetch
        self.cache_path = Path(cache_path)
        self.ttl = timedelta(hours=ttl_hours)
        self._mem = {}
        self._load_cache()

    def _load_cache(self):
        try:
            if self.cache_path.exists():
                self._mem = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            self._mem = {}

    def _save_cache(self):
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._mem, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _fresh(self, entry) -> bool:
        try:
            return datetime.utcnow() - datetime.fromisoformat(entry["fetched"]) < self.ttl
        except Exception:
            return False

    def next_earnings_date(self, ticker: str):
        """Return a date for the next earnings, or None if unknown/unavailable."""
        tkr = (ticker or "").upper()
        if not tkr:
            return None
        entry = self._mem.get(tkr)
        if entry and self._fresh(entry):
            d = entry.get("date")
            return date.fromisoformat(d) if d else None
        # refresh
        ds = None
        try:
            ds = self.fetch_fn(tkr)
        except Exception as exc:
            log.debug("earnings fetch_fn error for %s: %s", tkr, exc)
        self._mem[tkr] = {"date": ds, "fetched": datetime.utcnow().isoformat()}
        self._save_cache()
        return date.fromisoformat(ds) if ds else None

    def days_until_earnings(self, ticker: str, today: date = None):
        ed = self.next_earnings_date(ticker)
        if ed is None:
            return None
        today = today or date.today()
        return (ed - today).days

    def in_blackout(self, ticker: str, blackout_days: int, today: date = None):
        """Return (in_blackout, earnings_date). in_blackout is True when earnings
        is within [0, blackout_days] days out. Unknown earnings -> (False, None)."""
        ed = self.next_earnings_date(ticker)
        if ed is None:
            return False, None
        today = today or date.today()
        days = (ed - today).days
        return (0 <= days <= blackout_days), ed
