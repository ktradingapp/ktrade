#!/usr/bin/env python3
"""KTrade two-tier intraday watchlist (v13.5).

Implements the hybrid-scanner design: a DAILY (or pre-market) scan publishes the
top-N candidates to a small watchlist file; INTRADAY scans then restrict their
universe to that shortlist instead of re-scanning the whole ~200-name universe
every cycle. Two-tier = fewer API calls, less noise, and entries only on names the
slower, higher-quality daily scan already liked.

It is OFF by default (`KTRADE_WATCHLIST_MODE=off`) and changes nothing about what
trades when off. When ON:
  - a daily-interval scan calls `publish(top_tickers)`;
  - an intraday-interval scan filters its universe to `tickers()`;
  - if no FRESH watchlist exists, the intraday scan considers NO new entries
    (you must run a daily scan first) — exits/position management are unaffected,
    since they run before entry ranking.

Mirrors KTrade conventions: atomic temp+rename write, and a staleness gate (a
watchlist older than `KTRADE_WATCHLIST_MAX_AGE_HOURS`, default 8h, is ignored).
Stdlib-only.
"""
import json
import os
from datetime import datetime, timezone


def _default_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.getenv("KTRADE_WATCHLIST_FILE",
                     os.path.join(os.path.dirname(here), "data", "ktrade_intraday_watchlist.json"))


class IntradayWatchlist:
    def __init__(self, path=None, max_age_hours=None):
        self.path = path or _default_path()
        self.max_age_hours = (float(max_age_hours) if max_age_hours is not None
                              else float(os.getenv("KTRADE_WATCHLIST_MAX_AGE_HOURS", "8")))

    # -- write -------------------------------------------------------------
    def publish(self, tickers, source="daily"):
        """Atomically write the shortlist (deduped, upper-cased, order preserved)."""
        seen, clean = set(), []
        for t in tickers or []:
            u = str(t).upper().strip()
            if u and u not in seen:
                seen.add(u)
                clean.append(u)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "count": len(clean),
            "tickers": clean,
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.path)
        return payload

    # -- read --------------------------------------------------------------
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def age_hours(self):
        data = self.load()
        if not data or not data.get("ts"):
            return None
        try:
            ts = datetime.fromisoformat(data["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        except Exception:
            return None

    def is_fresh(self):
        age = self.age_hours()
        return age is not None and age <= self.max_age_hours

    def tickers(self):
        """Watchlist names, but ONLY if fresh; otherwise empty (enforces the
        'run a daily scan first' discipline rather than silently scanning all)."""
        if not self.is_fresh():
            return []
        data = self.load() or {}
        return [str(t).upper() for t in data.get("tickers", [])]

    # -- helper for the intraday scan path --------------------------------
    def filter_universe(self, data_map):
        """Return (filtered_data_map, reason). Keeps only fresh-watchlist names.
        Empty watchlist -> empty map (no new entries) with a reason string."""
        names = set(self.tickers())
        if not names:
            return {}, "no fresh intraday watchlist (run a daily scan first)"
        kept = {t: df for t, df in (data_map or {}).items() if str(t).upper() in names}
        return kept, f"restricted to {len(kept)} watchlist name(s)"


def mode_on():
    return os.getenv("KTRADE_WATCHLIST_MODE", "off").strip().lower() == "on"


def watchlist_size():
    try:
        return max(1, int(os.getenv("KTRADE_WATCHLIST_SIZE", "30")))
    except (TypeError, ValueError):
        return 30
