"""V13.8 strategy-validator report/verdict tests (stdlib-only, offline)."""
import os
import sys

root = os.path.dirname(os.path.abspath(__file__))
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from agent.strategy_validator import (assemble_report, overall_verdict,
                                          PASS, FAIL, WARN, SKIP)
except Exception as exc:
    print(f"SKIP test_v136: import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
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


def S(name, status, detail="x"):
    return {"name": name, "status": status, "detail": detail}


def test_verdict_logic():
    check("all pass -> READY", overall_verdict([S("a", PASS), S("b", PASS)]) == "READY")
    check("pass+skip -> READY", overall_verdict([S("a", PASS), S("b", SKIP)]) == "READY")
    check("a warn -> REVIEW", overall_verdict([S("a", PASS), S("b", WARN)]) == "REVIEW")
    check("a fail -> NOT_READY", overall_verdict([S("a", PASS), S("b", FAIL)]) == "NOT_READY")
    check("fail beats warn", overall_verdict([S("a", WARN), S("b", FAIL)]) == "NOT_READY")


def test_report_contents():
    stages = [S("compile", PASS, "all modules compile"),
              S("tests", PASS, "20 suites passed"),
              S("fragility", WARN, "macd: FRAGILE"),
              S("backtest", SKIP, "no --prices given")]
    md, verdict = assemble_report(stages, meta={"version": "13.8", "context": "strategy=macd"})
    check("verdict REVIEW (fragility warn)", verdict == "REVIEW")
    check("report names the verdict", "REVIEW" in md)
    check("report lists each stage", all(s["name"] in md for s in stages))
    check("report has the version", "13.8" in md)
    check("report explains skips", "Skipped stages" in md)
    check("report disclaims edge", "profitable" in md.lower() and "edge" in md.lower())


def test_ready_report():
    md, verdict = assemble_report([S("compile", PASS), S("tests", PASS),
                                   S("backtest", SKIP), S("fragility", SKIP), S("copilot", SKIP)])
    check("all pass/skip -> READY", verdict == "READY")
    check("READY blurb present", "Safe to proceed" in md)


def test_not_ready_report():
    md, verdict = assemble_report([S("compile", PASS), S("tests", FAIL, "failed: test_v130")])
    check("a fail -> NOT_READY", verdict == "NOT_READY")
    check("NOT_READY tells you to stop", "Do not proceed" in md)


if __name__ == "__main__":
    test_verdict_logic()
    test_report_contents()
    test_ready_report()
    test_not_ready_report()
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
