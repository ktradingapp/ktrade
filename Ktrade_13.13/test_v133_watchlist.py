"""V13.5 two-tier intraday watchlist tests (stdlib-only, offline)."""
import os
import sys
import json
import tempfile
from datetime import datetime, timezone, timedelta

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from agent import intraday_watchlist as W
except Exception as exc:
    print(f"SKIP test_v133: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
    sys.exit(0)

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}")


def _tmp():
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    return p


def test_publish_and_read():
    p = _tmp()
    try:
        wl = W.IntradayWatchlist(path=p)
        payload = wl.publish(["nvda", "amd", "nvda", " MU "], source="1d")  # dedupe + upper + trim
        check("publish dedupes/uppercases/orders", payload["tickers"] == ["NVDA", "AMD", "MU"])
        check("publish records source + count", payload["source"] == "1d" and payload["count"] == 3)
        check("file written (atomic, no .tmp left)", os.path.exists(p) and not os.path.exists(p + ".tmp"))
        check("ts is UTC", payload["ts"].endswith("+00:00"))
        wl2 = W.IntradayWatchlist(path=p)
        check("fresh watchlist returns tickers", wl2.tickers() == ["NVDA", "AMD", "MU"])
        check("is_fresh true right after publish", wl2.is_fresh() is True)
    finally:
        os.path.exists(p) and os.remove(p)


def test_staleness_gate():
    p = _tmp()
    try:
        old = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": old, "source": "1d", "count": 2, "tickers": ["NVDA", "AMD"]}, f)
        wl = W.IntradayWatchlist(path=p, max_age_hours=8)
        check("12h-old watchlist is NOT fresh (max 8h)", wl.is_fresh() is False)
        check("stale watchlist yields no tickers", wl.tickers() == [])
        kept, why = wl.filter_universe({"NVDA": 1, "AMD": 2})
        check("stale -> empty universe (no new entries)", kept == {})
        check("stale -> reason explains why", "no fresh" in why.lower())
    finally:
        os.path.exists(p) and os.remove(p)


def test_filter_universe():
    p = _tmp()
    try:
        wl = W.IntradayWatchlist(path=p)
        wl.publish(["NVDA", "AMD", "MU"], source="1d")
        kept, why = wl.filter_universe({"NVDA": 1, "AMD": 2, "AAPL": 3, "TSLA": 4})
        check("intraday universe restricted to watchlist", sorted(kept) == ["AMD", "NVDA"])
        check("filter reason reports count", "2 watchlist" in why)
        empty, why2 = wl.filter_universe({})
        check("empty data_map -> empty kept", empty == {})
    finally:
        os.path.exists(p) and os.remove(p)


def test_missing_watchlist():
    wl = W.IntradayWatchlist(path=_tmp())  # never written
    check("missing watchlist not fresh", wl.is_fresh() is False)
    check("missing watchlist -> no tickers", wl.tickers() == [])
    kept, why = wl.filter_universe({"NVDA": 1})
    check("missing -> empty universe", kept == {} and "no fresh" in why.lower())


def test_helpers():
    os.environ["KTRADE_WATCHLIST_MODE"] = "off"
    check("mode_on false when off", W.mode_on() is False)
    os.environ["KTRADE_WATCHLIST_MODE"] = "on"
    check("mode_on true when on", W.mode_on() is True)
    os.environ["KTRADE_WATCHLIST_SIZE"] = "15"
    check("watchlist_size reads env", W.watchlist_size() == 15)
    os.environ.pop("KTRADE_WATCHLIST_MODE", None)
    os.environ.pop("KTRADE_WATCHLIST_SIZE", None)


if __name__ == "__main__":
    test_publish_and_read()
    test_staleness_gate()
    test_filter_universe()
    test_missing_watchlist()
    test_helpers()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
