"""
test_v112_strategy_switch.py — offline tests for the regime strategy switch.
Run: python test_v112_strategy_switch.py   (no network)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "data"))
import strategy_selector as ss  # noqa: E402

BASE = 80.0


def run():
    passed = 0

    # ── Disabled => always baseline, regardless of regime/vix ────────────────
    os.environ["KTRADE_STRATEGY_SWITCH_ENABLED"] = "false"
    p = ss.select_profile("CRASH", vix=99, base_conviction=BASE)
    assert p.regime == "NEUTRAL" and p.min_conviction == BASE
    assert p.size_mult == 1.0 and p.allow_new_longs is True
    passed += 1

    # ── Enabled ──────────────────────────────────────────────────────────────
    os.environ["KTRADE_STRATEGY_SWITCH_ENABLED"] = "true"

    # BULL: momentum, lower bar, full size
    p = ss.select_profile("BULL", vix=15, base_conviction=BASE)
    assert p.name == "momentum" and p.min_conviction == BASE - 5 and p.size_mult == 1.0
    passed += 1

    # NEUTRAL: baseline
    p = ss.select_profile("NEUTRAL", vix=15, base_conviction=BASE)
    assert p.name == "balanced" and p.min_conviction == BASE
    passed += 1

    # RISK_OFF: defensive, higher bar, half size
    p = ss.select_profile("RISK_OFF", vix=20, base_conviction=BASE)
    assert p.name == "mean-reversion" and p.min_conviction == BASE + 8 and p.size_mult == 0.5
    passed += 1

    # CRASH: no new longs, size 0, impossible bar
    p = ss.select_profile("CRASH", vix=20, base_conviction=BASE)
    assert p.allow_new_longs is False and p.size_mult == 0.0 and p.min_conviction >= 999
    passed += 1

    # ── VIX overlay escalates a stale calm regime, never de-escalates ─────────
    # BULL but VIX 32 -> at least RISK_OFF
    p = ss.select_profile("BULL", vix=32, base_conviction=BASE)
    assert p.regime == "RISK_OFF", p.regime
    passed += 1

    # BULL but VIX 42 -> CRASH (block longs)
    p = ss.select_profile("BULL", vix=42, base_conviction=BASE)
    assert p.regime == "CRASH" and p.allow_new_longs is False
    passed += 1

    # BULL but VIX 26 -> floor NEUTRAL
    p = ss.select_profile("BULL", vix=26, base_conviction=BASE)
    assert p.regime == "NEUTRAL", p.regime
    passed += 1

    # RISK_OFF with calm VIX stays RISK_OFF (overlay never loosens)
    p = ss.select_profile("RISK_OFF", vix=10, base_conviction=BASE)
    assert p.regime == "RISK_OFF", p.regime
    passed += 1

    # Unknown regime label falls back to NEUTRAL baseline behaviour
    p = ss.select_profile("WAT", vix=15, base_conviction=BASE)
    assert p.regime == "NEUTRAL"
    passed += 1

    print(f"OK — {passed}/10 strategy-switch assertions passed.")


if __name__ == "__main__":
    run()
