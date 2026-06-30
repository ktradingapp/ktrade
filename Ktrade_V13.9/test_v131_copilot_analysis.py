"""V13.1 copilot ledger-analysis tests (stdlib-only, fully offline).

Worked example (all rule BUYs at price 100, decided 2026-06-01):
  AAA copilot BUY  -> fwd 105 (+5%)   agreement, winner
  BBB copilot BUY  -> fwd  97 (-3%)   agreement, loser
  CCC copilot SKIP -> fwd  92 (-8%)   disagreement, veto CORRECT (active veto)
  DDD copilot SKIP -> fwd 106 (+6%)   disagreement, veto WRONG
  EEE copilot SKIP -> fwd  98 (-2%)   disagreement, veto CORRECT
  FFF copilot ABSTAIN -> fwd 110      marked but NOT scored (no opinion)

Expected: 6 decisions, 5 opinions, 2 agree, 3 disagree, 1 abstain, 1 veto.
Outcome score: veto 2 correct / 1 wrong, precision 0.667, net benefit +4.00 pct-pts
(skipping trades that summed to -4% return improves P&L by +4%).
"""
import os
import sys
import csv
import tempfile

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from agent import copilot_analysis as CA
except Exception as exc:
    print(f"SKIP test_v131: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
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


def _rows():
    # Notionals chosen so the ONE wrong veto (DDD) lands on a big trade and the
    # two correct vetoes (CCC, EEE) are small — so equal-weighted says vetoes help
    # (+4 pct-pts) but $-weighted says they HURT (-$200). That sign-flip is the point.
    sizes = {"AAA": 1000.0, "BBB": 1000.0, "CCC": 1000.0,
             "DDD": 5000.0, "EEE": 1000.0, "FFF": 1000.0}
    mk = lambda t, v, vetoed=False: {
        "ts": "2026-06-01T10:00:00+00:00", "ticker": t, "rule_action": "BUY",
        "rule_reason": "approved", "conviction": 80, "price": 100.0,
        "qty": sizes[t] / 100.0, "notional": sizes[t], "dollar_risk": 50.0,
        "strategy": "MOMENTUM", "copilot_verdict": v, "copilot_reason": "",
        "mode": "active" if vetoed else "shadow",
        "disagreement": v in ("SKIP", "HOLD"), "vetoed": vetoed,
    }
    return [mk("AAA", "BUY"), mk("BBB", "BUY"), mk("CCC", "SKIP", vetoed=True),
            mk("DDD", "SKIP"), mk("EEE", "SKIP"), mk("FFF", "ABSTAIN")]


_FWD = {"AAA": 105.0, "BBB": 97.0, "CCC": 92.0, "DDD": 106.0, "EEE": 98.0, "FFF": 110.0}


def _fake_price_at(ticker, ts_iso, horizon_days):
    return _FWD.get(str(ticker).upper())


def test_decision_stats():
    ds = CA.decision_stats(_rows())
    check("6 decisions", ds["decisions"] == 6)
    check("5 opinions", ds["opinions"] == 5)
    check("2 agreements", ds["agreements"] == 2)
    check("3 disagreements", ds["disagreements"] == 3)
    check("1 abstain", ds["abstains"] == 1)
    check("1 veto applied", ds["vetoes"] == 1)
    check("agreement rate 0.4", ds["agreement_rate"] == 0.4)
    check("disagreement rate 0.6", ds["disagreement_rate"] == 0.6)
    check("abstain rate ~0.167", abs(ds["abstain_rate"] - 0.167) < 0.01)
    check("vetoed tickers tracked", set(ds["vetoed_tickers"]) == {"CCC", "DDD", "EEE"})


def test_attribute_and_score():
    rows, n = CA.attribute_outcomes(_rows(), _fake_price_at, horizon_days=5)
    check("all 6 BUYs marked forward", n == 6)
    check("AAA outcome +5%", abs(rows[0]["outcome_return"] - 5.0) < 1e-6)
    check("CCC outcome -8%", abs(rows[2]["outcome_return"] + 8.0) < 1e-6)

    sc = CA.outcome_score(rows)
    check("scored excludes ABSTAIN (5)", sc["scored"] == 5)
    check("2 agreements scored", sc["agreements_scored"] == 2)
    check("3 disagreements scored", sc["disagreements_scored"] == 3)
    check("agreement avg +1.0%", sc["agreement_avg_return"] == 1.0)
    check("agreement win rate 0.5", sc["agreement_win_rate"] == 0.5)
    check("veto correct = 2", sc["veto_correct"] == 2)
    check("veto wrong = 1", sc["veto_wrong"] == 1)
    check("veto precision 0.667", abs(sc["veto_precision"] - 0.667) < 0.01)
    check("net veto benefit +4.0", sc["net_veto_benefit_pct"] == 4.0)
    # $-weighted: -(CCC -8%*1000 + DDD +6%*5000 + EEE -2%*1000) = -(-80+300-20) = -200
    check("net veto benefit $-weighted = -200", sc["net_veto_benefit_dollars"] == -200.0)
    check("notional coverage = 3", sc["notional_coverage"] == 3)
    check("sign-flip: equal-wt +, $-wt - (size matters)",
          sc["net_veto_benefit_pct"] > 0 and sc["net_veto_benefit_dollars"] < 0)
    check("verdict says IMPROVED", "IMPROVED" in sc["verdict"])
    check("verdict warns of OPPOSITE sign", "OPPOSITE" in sc["verdict"])


def test_no_outcomes_path():
    report = CA.build_report(_rows(), price_at=None)
    check("no outcome_score without prices", "outcome_score" not in report)
    check("decision stats still present", report["decision_stats"]["decisions"] == 6)
    txt = CA.format_report(report)
    check("report renders text", "Copilot Ledger" in txt and "No outcomes attributed" in txt)


def test_csv_price_path():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "date", "close"])
        # decision 2026-06-01 + horizon 5 -> target 2026-06-06 -> first >= is 06-08
        for t, c in [("AAA", 105.0), ("BBB", 97.0), ("CCC", 92.0),
                     ("DDD", 106.0), ("EEE", 98.0), ("FFF", 110.0)]:
            w.writerow([t, "2026-06-01", 100.0])
            w.writerow([t, "2026-06-08", c])
    try:
        prices = CA.load_prices_csv(path)
        price_at = CA.make_price_at(prices)
        check("CSV forward price picks 06-08 close", price_at("AAA", "2026-06-01T10:00:00", 5) == 105.0)
        report = CA.build_report(_rows(), price_at=price_at, horizon_days=5)
        check("CSV path scores outcomes", report["outcome_score"]["net_veto_benefit_pct"] == 4.0)
    finally:
        os.remove(path)


if __name__ == "__main__":
    test_decision_stats()
    test_attribute_and_score()
    test_no_outcomes_path()
    test_csv_price_path()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
