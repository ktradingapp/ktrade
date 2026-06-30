"""
test_price_sanity.py — proves the v10.2 guard catches the KLAC failure.
Run: python data/test_price_sanity.py
"""
from price_sanity import PriceSanityGuard


def main():
    g = PriceSanityGuard()

    # 1) Learn a good reference for KLAC (~$236, the real exit-area price).
    clean = g.scrub({"KLAC": 236.68, "NVDA": 209.97})
    assert clean["KLAC"] == 236.68 and clean["NVDA"] == 209.97
    print("1) reference learned: KLAC=236.68 NVDA=209.97")

    # 2) The bad tick from the peer agent: $2300.52 (10x). Must be rejected and
    #    carried forward to last-good, NOT passed through.
    scrubbed = g.scrub({"KLAC": 2300.52})
    assert scrubbed["KLAC"] == 236.68, scrubbed
    assert g.flags and g.flags[-1]["reason"].startswith("decimal_shift")
    print(f"2) bad tick 2300.52 rejected as '{g.flags[-1]['reason']}', "
          f"carried forward to {scrubbed['KLAC']}")

    # 3) Hard entry gate: validate_entry must refuse the trade outright.
    decision = g.validate_entry("KLAC", 2300.52, reference=236.68)
    assert decision["ok"] is False
    print(f"3) validate_entry -> ok={decision['ok']} reason={decision['reason']}")

    # 4) A genuine move (+8%, like the real KLAC pop) must PASS.
    ok_move = g.validate_entry("KLAC", 236.68 * 1.08, reference=236.68)
    assert ok_move["ok"] is True
    print(f"4) genuine +8% move -> ok={ok_move['ok']} ({ok_move['reason']})")

    # 5) A 10x-too-low decimal error ($23.6) must also be caught.
    low = g.validate_entry("KLAC", 23.67, reference=236.68)
    assert low["ok"] is False and "decimal_shift" in low["reason"]
    print(f"5) 10x-low tick 23.67 -> ok={low['ok']} reason={low['reason']}")

    print("\nALL PRICE-SANITY TESTS PASSED")


if __name__ == "__main__":
    main()
