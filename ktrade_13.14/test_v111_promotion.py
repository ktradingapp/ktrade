"""
test_v111_promotion.py — offline tests for the paper->live promotion gate.
Run: python test_v111_promotion.py   (no network / broker needed)
"""
import os
import sys
import tempfile
from datetime import timezone, timedelta, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "risk"))
import promotion_gate as pg  # noqa: E402


def _fresh_gate(tmp):
    # Force JSON backend so the test is hermetic and needs no ktrade.db.
    os.environ["KTRADE_STATE_BACKEND"] = "json"
    g = pg.PromotionGate(db_path=os.path.join(tmp, "x.db"),
                         json_path=os.path.join(tmp, "ledger.json"))
    return g


def _backdate(g, symbol, days):
    """Push first_seen into the past so the trial-age gate can pass in a test."""
    rec = g._read(symbol)
    rec["first_seen"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    g._write(symbol, rec)


def run():
    passed = 0
    with tempfile.TemporaryDirectory() as tmp:
        # Deterministic thresholds for the test
        os.environ.update({
            "KTRADE_PROMOTION_ENABLED": "true",
            "KTRADE_PROMOTION_MIN_TRADES": "5",
            "KTRADE_PROMOTION_MIN_WIN_RATE": "0.55",
            "KTRADE_PROMOTION_TRIAL_DAYS": "5",
            "KTRADE_PROMOTION_REQUIRE_NET_POSITIVE": "true",
            "KTRADE_PROMOTION_MIN_AVG_PNL": "0.0",
        })

        g = _fresh_gate(tmp)

        # 1) Unknown symbol is never promoted.
        ok, why = g.is_promoted("NVDA")
        assert ok is False, why
        passed += 1

        # 2) Too few trades -> not promoted.
        for _ in range(3):
            g.record_closed_trade("NVDA", pnl=10.0)
        ok, why = g.is_promoted("NVDA")
        assert ok is False and "paper trades" in why, why
        passed += 1

        # 3) Enough winning trades but trial age not met -> still blocked.
        for _ in range(3):
            g.record_closed_trade("NVDA", pnl=10.0)   # now 6 trades, all wins
        ok, why = g.is_promoted("NVDA")
        assert ok is False and "trial age" in why, why
        passed += 1

        # 4) Backdate first_seen past the trial window -> graduates.
        _backdate(g, "NVDA", days=6)
        ok, why = g.is_promoted("NVDA")
        assert ok is True, why
        passed += 1

        # 5) Once promoted, stays promoted (sticky) and stamps promoted_at.
        assert g._read("NVDA").get("promoted_at"), "promoted_at not stamped"
        passed += 1

        # 6) A losing name with poor win-rate never graduates.
        for i in range(8):
            g.record_closed_trade("MEME", pnl=(-20.0 if i % 2 == 0 else 5.0))
        _backdate(g, "MEME", days=30)
        ok, why = g.is_promoted("MEME")
        assert ok is False, why
        passed += 1

        # 7) Gate semantics: PAPER mode never blocks (even un-graduated names).
        os.environ["LIVE_TRADING"] = "false"
        os.environ["KTRADE_LIVE_TRADING"] = "false"
        assert g.block_reason_if_live("MEME") is None
        passed += 1

        # 8) LIVE mode blocks an un-graduated name, allows a graduated one.
        os.environ["KTRADE_LIVE_TRADING"] = "true"
        assert g.block_reason_if_live("MEME") is not None
        assert g.block_reason_if_live("NVDA") is None
        passed += 1

        # 9) Disabled gate is a hard no-op (never blocks), regardless of live.
        os.environ["KTRADE_PROMOTION_ENABLED"] = "false"
        assert g.block_reason_if_live("MEME") is None
        passed += 1

        # 10) record_truth_trades is idempotent across repeated reconcile polls.
        os.environ["KTRADE_PROMOTION_ENABLED"] = "true"
        trips = [
            {"ticker": "AMD", "pnl": 8.0, "exit_time": "2026-06-20T15:00:00", "entry_time": "2026-06-20T10:00:00", "qty": 2},
            {"ticker": "AMD", "pnl": -3.0, "exit_time": "2026-06-21T15:00:00", "entry_time": "2026-06-21T10:00:00", "qty": 2},
        ]
        first = g.record_truth_trades(trips)
        again = g.record_truth_trades(trips)      # same poll data again
        assert first == 2, first
        assert again == 0, again                  # nothing new recorded
        assert g._read("AMD").get("trades") == 2, g._read("AMD")
        passed += 1

    print(f"OK — {passed}/10 promotion-gate assertions passed.")


if __name__ == "__main__":
    run()
