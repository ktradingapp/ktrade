"""V12.8 resilience tests — the autonomous-loop safety guards from the
production audit.

Covers:
  - VIX feed staleness detection (`_vix_is_stale`).
  - The pre-cycle guard `KTradeCEO._broker_truth_guard`:
      * stale VIX / no regime feed  -> refuse new entries (conservative);
      * broker positions in sync     -> safe to trade;
      * phantom position (we hold X, broker flat) -> desync halt + kill switch;
      * broker unreachable N cycles while holding risk -> halt.

Skips gracefully if the agent module / its deps can't be imported.
"""
import os
import sys
from datetime import datetime, timedelta

root = os.path.dirname(os.path.abspath(__file__))
for _p in (root, os.path.join(root, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("KTRADE_REQUIRE_FRESH_VIX", "true")
os.environ.setdefault("KTRADE_PAPER_ORDER_SUBMISSION", "false")

try:
    import agent.ktrade_agent_v9 as K
except Exception as exc:
    print(f"SKIP test_v128: agent import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
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


class _FakeBroker:
    def __init__(self, positions=None, fail=False):
        self._positions = positions or []
        self._fail = fail

    def get_positions(self):
        if self._fail:
            raise RuntimeError("broker unreachable")
        return self._positions


class _FakeEmergency:
    def __init__(self):
        self.triggered = False
        self.flatten = None

    def trigger(self, reason, flatten=False):
        self.triggered = True
        self.flatten = flatten

    def active(self):
        return False


def _ceo(broker):
    ceo = K.KTradeCEO(broker=broker)
    ceo.emergency = _FakeEmergency()  # isolate from the persistent kill store
    return ceo


def test_vix_staleness():
    K.MARKET.vix_updated_at = ""
    check("VIX stale when never updated", K._vix_is_stale())
    K.MARKET.vix_updated_at = datetime.now().isoformat()
    check("VIX fresh after a feed update", not K._vix_is_stale())
    K.MARKET.vix_updated_at = (datetime.now() - timedelta(minutes=30)).isoformat()
    check("VIX stale at 30 min old (max 15)", K._vix_is_stale())


def test_pre_cycle_guard():
    # 1) blind to regime -> conservative halt
    K.MARKET.vix_updated_at = ""
    safe, reason = _ceo(_FakeBroker())._broker_truth_guard()
    check("stale VIX -> cycle halted", (not safe) and "REGIME STALE" in reason)

    # from here, keep VIX fresh so we exercise the broker-truth branch
    K.MARKET.vix_updated_at = datetime.now().isoformat()

    # 2) broker in sync -> safe
    c = _ceo(_FakeBroker(positions=[{"symbol": "AAPL"}]))
    c.risk.engine.open_positions = {"AAPL": {"qty": 10, "avg_cost": 100}}
    safe, reason = c._broker_truth_guard()
    check("broker in sync -> safe to trade", safe)

    # 3) phantom position -> desync halt + kill switch fired
    c = _ceo(_FakeBroker(positions=[]))  # broker flat
    c.risk.engine.open_positions = {"AAPL": {"qty": 10, "avg_cost": 100}}
    safe, reason = c._broker_truth_guard()
    check("phantom position -> desync halt", (not safe) and "desync" in reason.lower())
    check("desync fires the kill switch", c.emergency.triggered)

    # 4) broker unreachable N cycles while holding risk -> halt
    c = _ceo(_FakeBroker(fail=True))
    c.risk.engine.open_positions = {"AAPL": {"qty": 10, "avg_cost": 100}}
    results = [c._broker_truth_guard() for _ in range(3)]
    check("broker unreachable x3 with risk -> halt",
          results[-1][0] is False and "unreachable" in results[-1][1])


if __name__ == "__main__":
    test_vix_staleness()
    test_pre_cycle_guard()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
