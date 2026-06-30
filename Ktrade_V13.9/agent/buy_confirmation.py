#!/usr/bin/env python3
"""KTrade N-cycle BUY confirmation (v13.7).

Fixes the single worst failure pattern seen in noisy LLM-driven agents: buying a
name off ONE scan, then flipping to SKIP on the very next cycle (the FCEL
bought-then-skipped case). The rule: a ticker must clear the hard gates + strategy
signal in N CONSECUTIVE scan cycles before it is allowed to execute. One-cycle
blips never reach the broker.

OFF by default (`KTRADE_BUY_CONFIRM_CYCLES=1` => every approved BUY is immediately
"confirmed", i.e. no behaviour change). Set to 2 to require two consecutive cycles.

HONEST TRADE-OFF: confirmation delays entries by (N-1) cycles, so it costs you the
first-cycle move. That's a reasonable trade for INTRADAY scanning (a 5m cycle = a
5-minute wait) but a poor one for DAILY scanning (a full day late, which hurts
momentum entries). This is an intraday-oriented feature; leave it off for daily.

The wall-clock gap guard (`KTRADE_BUY_CONFIRM_MAX_GAP_MINUTES`, default 20) breaks a
streak when too long passes between passes (e.g. overnight, or a restart) so a pass
from a previous session can't count toward today's confirmation. NOTE: this default
suits cycles <= 15m. For 30m/daily you'd have to raise it — another reason this is
meant for fast scanning. Pure in-memory; fully unit-tested offline.
"""
import os
from datetime import datetime, timezone


def required_cycles():
    try:
        return max(1, int(os.getenv("KTRADE_BUY_CONFIRM_CYCLES", "1")))
    except (TypeError, ValueError):
        return 1


def max_gap_minutes():
    try:
        return float(os.getenv("KTRADE_BUY_CONFIRM_MAX_GAP_MINUTES", "20"))
    except (TypeError, ValueError):
        return 20.0


class BuyConfirmation:
    def __init__(self, required=None, gap_minutes=None):
        self.required = int(required) if required is not None else required_cycles()
        self.gap_minutes = float(gap_minutes) if gap_minutes is not None else max_gap_minutes()
        self._streak = {}   # ticker -> {"count": int, "last": datetime}

    def enabled(self):
        return self.required > 1

    def register_pass(self, ticker, now=None):
        """Record that `ticker` cleared the gates this cycle; return its streak."""
        now = now or datetime.now(timezone.utc)
        t = str(ticker).upper()
        cur = self._streak.get(t)
        if cur is None:
            self._streak[t] = {"count": 1, "last": now}
        else:
            gap = (now - cur["last"]).total_seconds() / 60.0
            cur["count"] = 1 if gap > self.gap_minutes else cur["count"] + 1
            cur["last"] = now
        return self._streak[t]["count"]

    def is_confirmed(self, ticker):
        cur = self._streak.get(str(ticker).upper())
        return bool(cur and cur["count"] >= self.required)

    def streak(self, ticker):
        cur = self._streak.get(str(ticker).upper())
        return cur["count"] if cur else 0

    def end_cycle(self, approved_tickers):
        """Break the streak for any tracked ticker NOT approved this cycle."""
        seen = {str(t).upper() for t in (approved_tickers or set())}
        for t in list(self._streak):
            if t not in seen:
                del self._streak[t]
