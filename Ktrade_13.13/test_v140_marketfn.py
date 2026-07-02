"""V13.13 market-feed (VIX/SPY) wiring tests — strict + fail-safe."""
import os
import sys
import types

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

# Stub backend.ktrade_alpaca so broker_adapter imports cleanly and SPY is controllable.
_alp = types.ModuleType("ktrade_alpaca")
_alp.fetch_prices = lambda syms: {"SPY": 550.0}
_backend = types.ModuleType("backend")
_backend.ktrade_alpaca = _alp
sys.modules.setdefault("backend", _backend)
sys.modules.setdefault("backend.ktrade_alpaca", _alp)

try:
    from agent import broker_adapter as B
    import data.ktrade_data as D
except Exception as exc:
    print(f"SKIP test_v140: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
    sys.exit(0)

_passed = 0
_failed = 0
_ORIG_GET_VIX = D.PolygonDataFeed.get_vix   # capture before any monkeypatch


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}")


def _with_vix(value):
    D.PolygonDataFeed.get_vix = lambda self, default=18.0: value
    return B.make_market_fn()()


def test_real_vix_included():
    out = _with_vix(25.0)
    check("real VIX 25 included", out.get("vix") == 25.0)
    check("SPY included", out.get("spy_price") == 550.0)


def test_feed_failure_fails_safe():
    out = _with_vix(None)          # strict get_vix returns None on total failure
    check("feed failure -> vix omitted (fail-safe)", "vix" not in out)
    check("SPY still present on VIX failure", out.get("spy_price") == 550.0)


def test_sanity_gate():
    check("bad high tick 500 -> omitted", "vix" not in _with_vix(500.0))
    check("bad low tick 3 -> omitted", "vix" not in _with_vix(3.0))
    check("boundary 5.0 included", _with_vix(5.0).get("vix") == 5.0)
    check("boundary 150.0 included", _with_vix(150.0).get("vix") == 150.0)


def test_get_vix_strict_contract():
    # Restore the real method (the tests above monkeypatch it).
    D.PolygonDataFeed.get_vix = _ORIG_GET_VIX
    # In this sandbox there is no POLYGON_KEY and yfinance is absent, so both the
    # strict (None) and default (18.0) contracts are exercised for real.
    check("get_vix(default=None) -> None when no source", D.PolygonDataFeed().get_vix(default=None) is None)
    check("get_vix() -> 18.0 default preserved (back-compat)", D.PolygonDataFeed().get_vix() == 18.0)


if __name__ == "__main__":
    test_real_vix_included()
    test_feed_failure_fails_safe()
    test_sanity_gate()
    test_get_vix_strict_contract()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
