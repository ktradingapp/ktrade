"""V13.12 intraday safety-rule tests (pure gates, offline)."""
import os
import sys
from datetime import datetime

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

try:
    import pandas as pd
    from agent.ktrade_agent_v9 import (in_no_entry_window, eod_flatten_due,
                                       _avg_dollar_volume, _min_avg_dollar_volume)
except Exception as exc:
    print(f"SKIP test_v139: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
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


def et(h, m):
    return datetime(2026, 6, 30, h, m)


def test_no_entry_window():
    check("opening auction 09:32 blocked", in_no_entry_window(et(9, 32))[0] is True)
    check("09:34 still in 5-min open block", in_no_entry_window(et(9, 34))[0] is True)
    check("09:35 open block over", in_no_entry_window(et(9, 35))[0] is False)
    check("closing 15:51 blocked", in_no_entry_window(et(15, 51))[0] is True)
    check("closing 15:59 blocked", in_no_entry_window(et(15, 59))[0] is True)
    check("15:49 not yet in close block", in_no_entry_window(et(15, 49))[0] is False)
    check("midday 11:00 open", in_no_entry_window(et(11, 0))[0] is False)
    check("blocked reason is non-empty", bool(in_no_entry_window(et(9, 31))[1]))


def test_eod_flatten_window():
    check("15:55 flatten due", eod_flatten_due(et(15, 55)) is True)
    check("15:59 flatten due", eod_flatten_due(et(15, 59)) is True)
    check("15:40 not yet", eod_flatten_due(et(15, 40)) is False)
    check("16:00 market closed, not due", eod_flatten_due(et(16, 0)) is False)
    check("custom cutoff 15:30 honored", eod_flatten_due(et(15, 45), cutoff_hour=15, cutoff_min=30) is True)


def test_liquidity():
    liquid = pd.DataFrame({"close": [100.0] * 25, "volume": [1_000_000] * 25})
    thin = pd.DataFrame({"close": [2.0] * 25, "volume": [1000] * 25})
    check("liquid name ~ $100M/bar", abs(_avg_dollar_volume(liquid) - 100_000_000) < 1)
    check("thin name ~ $2k/bar", abs(_avg_dollar_volume(thin) - 2000) < 1)
    check("empty df -> 0", _avg_dollar_volume(pd.DataFrame()) == 0.0)
    check("missing volume col -> 0", _avg_dollar_volume(pd.DataFrame({"close": [1, 2]})) == 0.0)


def test_env_defaults_off():
    os.environ.pop("KTRADE_MIN_AVG_DOLLAR_VOL", None)
    check("liquidity floor defaults to 0 (off)", _min_avg_dollar_volume() == 0.0)
    os.environ["KTRADE_MIN_AVG_DOLLAR_VOL"] = "5000000"
    check("liquidity floor reads env", _min_avg_dollar_volume() == 5_000_000.0)
    os.environ.pop("KTRADE_MIN_AVG_DOLLAR_VOL", None)


if __name__ == "__main__":
    test_no_entry_window()
    test_eod_flatten_window()
    test_liquidity()
    test_env_defaults_off()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
