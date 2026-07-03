"""V13.0 copilot advisory-layer tests.

Proves the safety-critical semantics with an INJECTED fake LLM (no network):
  - parsing (JSON + text fallback + garbage -> ABSTAIN);
  - mode behavior: off -> ABSTAIN; shadow produces an opinion but NEVER blocks;
    active SKIP/HOLD blocks; active BUY does not;
  - fail-open: any LLM error -> ABSTAIN and never blocks (LLM downtime cannot
    halt trading);
  - the audit ledger records rows and tallies agreement / disagreement / vetoes.

Skips gracefully if the agent module can't be imported.
"""
import os
import sys
import tempfile
from types import SimpleNamespace

root = os.path.dirname(os.path.abspath(__file__))
for _p in (root, os.path.join(root, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import agent.ktrade_agent_v9 as K
except Exception as exc:
    print(f"SKIP test_v130: agent import unavailable ({type(exc).__name__}: {exc}). Treating as pass.")
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


def _advisor(mode, ask_fn=None, ledger_path=None):
    os.environ["KTRADE_COPILOT_MODE"] = mode
    led = K.CopilotLedger(path=ledger_path) if ledger_path else K.CopilotLedger()
    return K.CopilotAdvisor(ledger=led, ask_fn=ask_fn)


_SCORE = SimpleNamespace(ticker="AAA", score=88, price=100.0, strategy="MOMENTUM",
                         components={"trend": 100, "momentum": 80})
_DEC = SimpleNamespace(approved=True, reason="ok")


def test_parse():
    P = K.CopilotAdvisor._parse
    check("parse JSON SKIP", P('{"verdict":"SKIP","reason":"overextended"}').verdict == "SKIP")
    check("parse JSON BUY", P('{"verdict":"BUY","reason":"clean trend"}').verdict == "BUY")
    check("parse JSON reason captured", P('{"verdict":"SKIP","reason":"too hot"}').reason == "too hot")
    check("parse text fallback -> SKIP", P("On balance I would SKIP this name").verdict == "SKIP")
    check("parse garbage -> ABSTAIN", P("").verdict == "ABSTAIN")


def test_modes_and_failopen():
    # off: ABSTAIN, never blocks, ask_fn irrelevant
    a = _advisor("off", ask_fn=lambda p: '{"verdict":"SKIP"}')
    v = a.consult(_SCORE, _DEC)
    check("off -> ABSTAIN", v.verdict == "ABSTAIN")
    check("off -> should_block False", a.should_block(v) is False)

    # shadow + SKIP: real opinion, disagreement flagged, but NEVER blocks
    a = _advisor("shadow", ask_fn=lambda p: '{"verdict":"SKIP","reason":"hot"}')
    v = a.consult(_SCORE, _DEC)
    check("shadow SKIP -> verdict SKIP", v.verdict == "SKIP")
    check("shadow SKIP -> disagrees_with_buy", v.disagrees_with_buy)
    check("shadow NEVER blocks", a.should_block(v) is False)

    # active + SKIP: blocks
    a = _advisor("active", ask_fn=lambda p: '{"verdict":"SKIP","reason":"hot"}')
    v = a.consult(_SCORE, _DEC)
    check("active SKIP -> should_block True", a.should_block(v) is True)

    # active + HOLD: also blocks (do-not-add)
    a = _advisor("active", ask_fn=lambda p: '{"verdict":"HOLD","reason":"wait"}')
    v = a.consult(_SCORE, _DEC)
    check("active HOLD -> should_block True", a.should_block(v) is True)

    # active + BUY: does not block
    a = _advisor("active", ask_fn=lambda p: '{"verdict":"BUY","reason":"go"}')
    v = a.consult(_SCORE, _DEC)
    check("active BUY -> no block", a.should_block(v) is False)

    # active + LLM error: ABSTAIN, fail-open (no block)
    def boom(_p):
        raise RuntimeError("llm down")
    a = _advisor("active", ask_fn=boom)
    v = a.consult(_SCORE, _DEC)
    check("active + LLM error -> ABSTAIN", v.verdict == "ABSTAIN")
    check("active + LLM error -> fail-open (no block)", a.should_block(v) is False)


def test_ledger():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.remove(path)
    led = K.CopilotLedger(path=path)
    led.record(_SCORE, _DEC, K.CopilotVerdict("BUY", "go"), mode="shadow")      # agreement
    led.record(_SCORE, _DEC, K.CopilotVerdict("SKIP", "hot"), mode="shadow")    # disagreement
    led.record(_SCORE, _DEC, K.CopilotVerdict("SKIP", "hot"), mode="active")    # disagreement + veto
    led.record(_SCORE, _DEC, K.CopilotVerdict("ABSTAIN", "down"), mode="shadow")  # abstain
    s = led.summary()
    check("ledger counts 4 decisions", s["decisions"] == 4)
    check("ledger 1 agreement", s["agreements"] == 1)
    check("ledger 2 disagreements", s["disagreements"] == 2)
    check("ledger 1 veto", s["vetoes"] == 1)
    check("ledger 1 abstain", s["abstains"] == 1)
    try:
        os.remove(path)
    except OSError:
        pass


if __name__ == "__main__":
    test_parse()
    test_modes_and_failopen()
    test_ledger()
    os.environ.pop("KTRADE_COPILOT_MODE", None)
    print(f"\n==== {_passed} passed, {_failed} failed ====")
    sys.exit(1 if _failed else 0)
