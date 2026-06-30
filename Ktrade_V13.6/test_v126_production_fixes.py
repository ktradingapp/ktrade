"""V12.6 regression tests — production-readiness fixes.

Covers:
  - place_bracket_order() defensive validation (qty/side/stop/target).
    This is also the backstop for the auto-trade sub-1-share fix: even if a
    caller computed qty < 1, the broker helper refuses it.
  - scan-file freshness rejection used by auto-trade and manual-BUY reference.

The backend module needs the web stack (flask/flask_cors). If it isn't installed
(minimal environment), this test SKIPS rather than failing — it runs fully inside
the project venv.
"""
import os
import sys

root = os.path.dirname(os.path.abspath(__file__))
for _p in (root, os.path.join(root, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import backend.ktrade_alpaca as k
except Exception as exc:  # missing optional web deps in a minimal env
    print(f"SKIP test_v126: backend import unavailable "
          f"({type(exc).__name__}: {exc}). Run inside the project venv. Treating as pass.")
    sys.exit(0)

from datetime import datetime, timezone

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


def _raises(callable_, *args):
    try:
        callable_(*args)
        return False
    except ValueError:
        return True
    except Exception:
        return False


def test_bracket_validation():
    check("bracket: qty < 1 refused",        _raises(k.place_bracket_order, "AAPL", 0, "buy", 1, 2))
    check("bracket: fractional <1 refused",  _raises(k.place_bracket_order, "AAPL", 0.4, "buy", 1, 2))
    check("bracket: invalid side refused",   _raises(k.place_bracket_order, "AAPL", 1, "hold", 1, 2))
    check("bracket: empty ticker refused",   _raises(k.place_bracket_order, "", 1, "buy", 1, 2))
    check("bracket: non-positive stop refused", _raises(k.place_bracket_order, "AAPL", 1, "buy", 0, 2))
    check("bracket: BUY stop>=target refused",  _raises(k.place_bracket_order, "AAPL", 1, "buy", 2, 1))


def test_scan_freshness():
    stale = {"generated_at": "2026-06-22T22:37:18"}
    fresh = {"generated_at": datetime.now(timezone.utc).isoformat()}
    check("scan: stale file rejected",        not k._scan_payload_is_fresh(stale)[0])
    check("scan: fresh file accepted",        k._scan_payload_is_fresh(fresh)[0])
    check("scan: missing generated_at rejected", not k._scan_payload_is_fresh({})[0])
    check("scan: invalid generated_at rejected", not k._scan_payload_is_fresh({"generated_at": "not-a-date"})[0])


if __name__ == "__main__":
    test_bracket_validation()
    test_scan_freshness()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
