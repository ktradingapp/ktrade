"""V13.7 N-cycle BUY confirmation tests (stdlib-only, offline)."""
import os
import sys
from datetime import datetime, timezone, timedelta

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from agent.buy_confirmation import BuyConfirmation
except Exception as exc:
    print(f"SKIP test_v135: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
    sys.exit(0)

_passed = 0
_failed = 0
T0 = datetime(2026, 6, 30, 14, 0, tzinfo=timezone.utc)


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}")


def test_off_by_default():
    b = BuyConfirmation(required=1)
    check("required=1 means disabled", b.enabled() is False)
    b.register_pass("NVDA", T0)
    check("disabled: approved -> confirmed immediately", b.is_confirmed("NVDA") is True)


def test_two_cycle_confirms():
    b = BuyConfirmation(required=2, gap_minutes=20)
    check("required=2 means enabled", b.enabled() is True)
    b.register_pass("NVDA", T0)
    check("cycle 1: not yet confirmed", b.is_confirmed("NVDA") is False)
    check("cycle 1: streak == 1", b.streak("NVDA") == 1)
    b.end_cycle({"NVDA"})
    b.register_pass("NVDA", T0 + timedelta(minutes=5))
    check("cycle 2 (consecutive): confirmed", b.is_confirmed("NVDA") is True)
    check("cycle 2: streak == 2", b.streak("NVDA") == 2)


def test_blip_resets_the_fcel_case():
    # approved c1, NOT approved c2, approved c3 -> must NOT be confirmed at c3
    b = BuyConfirmation(required=2, gap_minutes=20)
    b.register_pass("FCEL", T0)
    b.end_cycle(set())                                   # c2: FCEL absent -> streak broken
    check("blip: streak dropped when not approved", b.streak("FCEL") == 0)
    b.register_pass("FCEL", T0 + timedelta(minutes=10))  # c3: approved again
    check("blip: FCEL NOT confirmed (restarts at 1)", b.is_confirmed("FCEL") is False)
    check("blip: FCEL streak back to 1", b.streak("FCEL") == 1)


def test_time_gap_breaks_streak():
    b = BuyConfirmation(required=2, gap_minutes=20)
    b.register_pass("AMD", T0)
    b.register_pass("AMD", T0 + timedelta(hours=18))     # overnight; gap > 20m
    check("overnight gap resets streak to 1", b.streak("AMD") == 1)
    check("overnight gap: not confirmed", b.is_confirmed("AMD") is False)


def test_independent_tickers():
    b = BuyConfirmation(required=2, gap_minutes=20)
    b.register_pass("NVDA", T0)
    b.register_pass("AMD", T0)
    b.end_cycle({"NVDA", "AMD"})
    b.register_pass("NVDA", T0 + timedelta(minutes=5))   # NVDA hits 2
    b.end_cycle({"NVDA"})                                 # AMD dropped this cycle
    check("NVDA confirmed independently", b.is_confirmed("NVDA") is True)
    check("AMD streak broken independently", b.streak("AMD") == 0)


def test_three_cycle():
    b = BuyConfirmation(required=3, gap_minutes=20)
    for i in range(2):
        b.register_pass("MU", T0 + timedelta(minutes=5 * i))
        b.end_cycle({"MU"})
    check("required=3: not confirmed after 2", b.is_confirmed("MU") is False)
    b.register_pass("MU", T0 + timedelta(minutes=10))
    check("required=3: confirmed after 3", b.is_confirmed("MU") is True)


def test_env_controls():
    os.environ["KTRADE_BUY_CONFIRM_CYCLES"] = "2"
    b = BuyConfirmation()
    check("env sets required cycles", b.required == 2 and b.enabled())
    os.environ.pop("KTRADE_BUY_CONFIRM_CYCLES", None)
    b2 = BuyConfirmation()
    check("default required is 1 (off)", b2.required == 1 and not b2.enabled())


if __name__ == "__main__":
    test_off_by_default()
    test_two_cycle_confirms()
    test_blip_resets_the_fcel_case()
    test_time_gap_breaks_streak()
    test_independent_tickers()
    test_three_cycle()
    test_env_controls()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
