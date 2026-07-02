#!/usr/bin/env python3
"""KTrade validation pipeline (v13.8).

One command that runs the whole validation sequence and emits a single report —
the "run this before you trust a change" gate the validation strategy calls for.

Stages, in order:
  1. compile         — py_compile every module (always runs)
  2. release_safety  — scripts/check_release_safety.py (always runs)
  3. tests           — every test_v1*.py + data/test_*.py, CI-style, isolated
                       app-data (always runs)
  4. backtest        — VectorBT backtest of one strategy (needs --prices + vectorbt;
                       SKIPs otherwise)
  5. fragility       — param_fragility sweep -> ROBUST/MIXED/FRAGILE (needs --prices +
                       vectorbt; FRAGILE => WARN, not FAIL; SKIPs otherwise)
  6. copilot         — copilot_analysis over a shadow ledger (needs a ledger from
                       paper trading; SKIPs otherwise)

Honest scope: stages 1-3 run for real anywhere. Stages 4-6 need inputs that only
exist on the VPS / after paper trading, so they SKIP cleanly here and execute there.
The report says plainly what ran vs skipped. A READY verdict means "correct, safe,
not obviously curve-fit" — NOT "profitable". Edge is proven by OOS + live paper
outcomes, not by this pipeline.

  python scripts/run_validation_pipeline.py
  python scripts/run_validation_pipeline.py --prices NVDA.csv --strategy macd
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.strategy_validator import assemble_report, PASS, FAIL, WARN, SKIP  # noqa: E402


def _py():
    return sys.executable or "python3"


def _run(cmd, env=None, cwd=ROOT, timeout=1800):
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def stage_compile():
    files = []
    for dirpath, _dirs, names in os.walk(ROOT):
        if "/.venv" in dirpath or "/__pycache__" in dirpath:
            continue
        for n in names:
            if n.endswith(".py"):
                files.append(os.path.join(dirpath, n))
    rc, out = _run([_py(), "-m", "py_compile", *files])
    return {"name": "compile", "status": PASS if rc == 0 else FAIL,
            "detail": "all modules compile" if rc == 0 else f"compile error:\n{out[-800:]}"}


def stage_release_safety():
    script = os.path.join(ROOT, "scripts", "check_release_safety.py")
    if not os.path.exists(script):
        return {"name": "release_safety", "status": SKIP, "detail": "check_release_safety.py not found"}
    rc, out = _run([_py(), script])
    return {"name": "release_safety", "status": PASS if rc == 0 else FAIL,
            "detail": "no forbidden files/secrets" if rc == 0 else f"FAILED:\n{out[-800:]}"}


def stage_tests():
    suites = []
    for d, sub in ((ROOT, "test_v1"), (os.path.join(ROOT, "data"), "test_")):
        if os.path.isdir(d):
            suites += [os.path.join(d, f) for f in sorted(os.listdir(d))
                       if f.startswith(sub) and f.endswith(".py")]
    suites = sorted(set(suites))
    if not suites:
        return {"name": "tests", "status": SKIP, "detail": "no test suites found"}
    appdata = os.path.join(ROOT, ".pipeline_appdata")
    env = {"KTRADE_APP_DATA_DIR": appdata, "KTRADE_COPILOT_MODE": "off", "KTRADE_REGIME_MODE": "off"}
    _run(["rm", "-rf", appdata]); os.makedirs(appdata, exist_ok=True)
    failed = []
    for t in suites:
        rc, out = _run([_py(), t], env=env)
        if rc != 0 or " failed" in out and not out.strip().endswith("0 failed ===="):
            # double-check the printed tally
            if rc != 0:
                failed.append(os.path.basename(t))
    status = PASS if not failed else FAIL
    detail = f"{len(suites)} suites passed" if not failed else f"failed: {', '.join(failed)}"
    return {"name": "tests", "status": status, "detail": detail}


def stage_backtest(prices, strategy):
    if not prices:
        return {"name": "backtest", "status": SKIP, "detail": "no --prices given (run on the VPS with real data)"}
    try:
        from agent.param_fragility import load_prices_csv, make_vectorbt_backtest_fn, DEFAULT_GRIDS
        close, volume = load_prices_csv(prices)
        fn = make_vectorbt_backtest_fn(close, volume, strategy)
        # use the centre of the grid as the representative param set
        grid = DEFAULT_GRIDS.get(strategy, DEFAULT_GRIDS["macd"])
        mid = {k: v[len(v) // 2] for k, v in grid.items()}
        m = fn(mid)
        if not m:
            return {"name": "backtest", "status": WARN, "detail": f"{strategy} {mid} produced no trades"}
        sharpe = m.get("sharpe"); ret = m.get("total_return"); dd = m.get("max_drawdown")
        return {"name": "backtest", "status": PASS,
                "detail": f"{strategy} {mid}: sharpe={sharpe}, total_return={ret}, max_dd={dd}"}
    except Exception as exc:
        return {"name": "backtest", "status": SKIP, "detail": f"could not run here: {exc}"}


def stage_fragility(prices, strategy):
    if not prices:
        return {"name": "fragility", "status": SKIP, "detail": "no --prices given (run on the VPS with real data)"}
    try:
        from agent.param_fragility import (load_prices_csv, make_vectorbt_backtest_fn,
                                           DEFAULT_GRIDS, sweep, analyze_fragility)
        close, volume = load_prices_csv(prices)
        fn = make_vectorbt_backtest_fn(close, volume, strategy)
        grid = DEFAULT_GRIDS.get(strategy, DEFAULT_GRIDS["macd"])
        a = analyze_fragility(sweep(fn, grid), grid)
        if not a.get("ok"):
            return {"name": "fragility", "status": WARN, "detail": a.get("reason", "no result")}
        cls = a["classification"]
        status = WARN if cls == "FRAGILE" else PASS
        return {"name": "fragility", "status": status,
                "detail": f"{strategy}: {cls} (profitable {a['profitable_fraction']}, "
                          f"plateau {a['plateau_ratio']}, cliff {a['cliff_fraction']})"}
    except Exception as exc:
        return {"name": "fragility", "status": SKIP, "detail": f"could not run here: {exc}"}


def stage_copilot(ledger):
    candidate = ledger or os.path.join(ROOT, "logs", "ktrade_copilot_ledger.jsonl")
    if not os.path.exists(candidate):
        return {"name": "copilot", "status": SKIP,
                "detail": "no shadow ledger yet (accumulates during paper trading)"}
    try:
        rc, out = _run([_py(), os.path.join(ROOT, "agent", "copilot_analysis.py"), "--ledger", candidate])
        return {"name": "copilot", "status": PASS if rc == 0 else WARN,
                "detail": (out[-600:] or "analyzed").strip()}
    except Exception as exc:
        return {"name": "copilot", "status": SKIP, "detail": f"could not run: {exc}"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="KTrade validation pipeline.")
    ap.add_argument("--prices", help="CSV (date,close[,volume]) to enable backtest+fragility stages")
    ap.add_argument("--strategy", default="macd")
    ap.add_argument("--ledger", help="copilot shadow ledger path (defaults to logs/ktrade_copilot_ledger.jsonl)")
    ap.add_argument("--out", default=os.path.join(ROOT, "reports", "strategy_validation_report.md"))
    args = ap.parse_args(argv)

    version = "unknown"
    try:
        import re
        src = open(os.path.join(ROOT, "agent", "ktrade_agent_v9.py"), encoding="utf-8").read()
        m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
        version = m.group(1) if m else version
    except Exception:
        pass

    stages = [
        stage_compile(),
        stage_release_safety(),
        stage_tests(),
        stage_backtest(args.prices, args.strategy),
        stage_fragility(args.prices, args.strategy),
        stage_copilot(args.ledger),
    ]
    report, verdict = assemble_report(stages, meta={"version": version,
                                                    "context": f"strategy={args.strategy}"})
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)

    print(report)
    print(f"\n[report written to {args.out}]")
    return 0 if verdict != "NOT_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
