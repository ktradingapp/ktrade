"""
test_v113_safety_spine.py — offline tests for the Milestone-1 safety spine.
Run: python test_v113_safety_spine.py   (no network)
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "risk"))
import portfolio_context as pc  # noqa: E402
import audit_log as al  # noqa: E402


def _write_snapshot(path, net_worth, as_of=None, secret=None, extra=None):
    body = {
        "net_worth": net_worth, "liquid": net_worth * 0.3,
        "committed": net_worth * 0.7, "trading_allocation": 100000.0,
        "as_of": (as_of or datetime.now(timezone.utc)).isoformat(),
    }
    if extra:
        body.update(extra)
    if secret:
        body["sig"] = hmac.new(secret.encode(), pc._canonical(body).encode(),
                               hashlib.sha256).hexdigest()
    with open(path, "w") as fh:
        json.dump(body, fh)


def run():
    passed = 0
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "portfolio_snapshot.json")
        os.environ.update({
            "KTRADE_PORTFOLIO_SNAPSHOT": snap,
            "KTRADE_PORTFOLIO_STATE": os.path.join(tmp, "portfolio_state.json"),
            "KTRADE_PORTFOLIO_MAX_POSITION_PCT": "5",
            "KTRADE_PORTFOLIO_MAX_DEPLOYED_PCT": "30",
            "KTRADE_NETWORTH_DD_HALT_PCT": "15",
            "KTRADE_PORTFOLIO_MAX_STALE_MIN": "60",
            "KTRADE_PORTFOLIO_FAIL_MODE": "block",
            "KTRADE_PORTFOLIO_HMAC_SECRET": "",
        })
        g = pc.PortfolioGate()

        # 1) Disabled -> all gates allow.
        os.environ["KTRADE_PORTFOLIO_GATE_ENABLED"] = "false"
        assert g.cycle_gate()[0] is True
        assert g.exposure_ok(999999, 0)[0] is True
        passed += 1

        # 2) Enabled + missing snapshot -> fail safe (block).
        os.environ["KTRADE_PORTFOLIO_GATE_ENABLED"] = "true"
        assert g.cycle_gate()[0] is False
        passed += 1

        # 3) Fresh valid snapshot -> cycle allowed.
        _write_snapshot(snap, 540000.0)
        ok, why = g.cycle_gate()
        assert ok is True, why
        passed += 1

        # 4) Exposure caps: 5% of 540k = 27k position cap.
        assert g.exposure_ok(20000, 0)[0] is True            # within position cap
        assert g.exposure_ok(30000, 0)[0] is False           # > 5% single-position
        # 30% of 540k = 162k deployed cap.
        assert g.exposure_ok(20000, 150000)[0] is False      # would exceed deployed cap
        assert g.exposure_ok(10000, 150000)[0] is True
        passed += 1

        # 5) Net-worth drawdown breaker: peak 540k, drop to 450k (16.7%) -> halt.
        _write_snapshot(snap, 450000.0)
        ok, why = g.cycle_gate()
        assert ok is False and "drawdown" in why, why
        passed += 1

        # 6) Bad-data jump guard: implausible swing vs last good -> rejected.
        _write_snapshot(snap, 50000.0)   # ~89% drop vs 450k last good
        assert g.get_context() is None
        passed += 1

        # 7) Staleness breaker: old snapshot -> fail safe.
        _write_snapshot(snap, 460000.0,
                        as_of=datetime.now(timezone.utc) - timedelta(hours=3))
        ctx = g.get_context()
        assert ctx is not None and ctx.stale is True
        assert g.cycle_gate()[0] is False
        passed += 1

        # 8) HMAC signature: correctly signed accepted, tampered rejected.
        secret = "shared-secret-xyz"
        os.environ["KTRADE_PORTFOLIO_HMAC_SECRET"] = secret
        _write_snapshot(snap, 470000.0, secret=secret)
        assert g.get_context() is not None                   # valid sig
        # tamper net_worth without re-signing
        body = json.loads(open(snap).read()); body["net_worth"] = 999999.0
        open(snap, "w").write(json.dumps(body))
        assert g.get_context() is None                       # sig now invalid
        os.environ["KTRADE_PORTFOLIO_HMAC_SECRET"] = ""
        passed += 1

        # ── Audit log ────────────────────────────────────────────────────────
        os.environ["KTRADE_AUDIT_ENABLED"] = "true"
        os.environ["KTRADE_AUDIT_PATH"] = os.path.join(tmp, "audit.jsonl")
        a = al.AuditLog()

        # 9) Records chain and verify() passes.
        a.record("submitted", {"ticker": "NVDA", "qty": 3})
        a.record("blocked", {"ticker": "MEME", "reason": "conviction"})
        a.record("cycle_halt", {"reason": "net-worth drawdown"})
        ok, msg = a.verify()
        assert ok is True, msg
        passed += 1

        # 10) Tampering a record breaks the chain.
        lines = open(os.environ["KTRADE_AUDIT_PATH"]).read().splitlines()
        rec = json.loads(lines[1]); rec["payload"]["reason"] = "edited"
        lines[1] = json.dumps(rec, separators=(",", ":"))
        open(os.environ["KTRADE_AUDIT_PATH"], "w").write("\n".join(lines) + "\n")
        ok, msg = a.verify()
        assert ok is False and "seq 1" in msg, msg
        passed += 1

    print(f"OK — {passed}/10 safety-spine assertions passed.")


if __name__ == "__main__":
    run()
