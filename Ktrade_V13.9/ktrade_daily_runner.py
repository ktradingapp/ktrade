#!/usr/bin/env python3
"""
KTrade daily/intraday integrated runner with permanent scanner provider fallback.

Primary provider can be Finnhub, but if scanner fails or does not create a fresh
scan output, it retries with fallback providers such as yfinance.

Examples:
  python ktrade_daily_runner.py status
  KTRADE_DATA_PROVIDER=finnhub python ktrade_daily_runner.py intraday --force
  KTRADE_DATA_PROVIDER=finnhub python ktrade_daily_runner.py daily --force

Useful env:
  KTRADE_DATA_PROVIDER=finnhub|yfinance|alpaca
  KTRADE_SCANNER_FALLBACKS=finnhub,yfinance
  KTRADE_SCAN_UNIVERSE=extended
  KTRADE_DAILY_SCAN_INTERVAL=1d
  KTRADE_INTRADAY_SCAN_INTERVAL=5m
  KTRADE_SKIP_NON_TRADING_DAYS=true
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "ktrade_daily_runner.log"
SCAN_FILE = ROOT / "data" / "ktrade_scan_latest.json"
DAILY_BT_FILE = ROOT / "data" / "ktrade_backtest_latest.json"
INTRADAY_BT_FILE = ROOT / "data" / "ktrade_intraday_backtest_latest.json"

US_MARKET_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in US_MARKET_HOLIDAYS_2026


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.utcnow().isoformat(timespec='seconds')}Z {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def py() -> str:
    return sys.executable or "python"


def run_cmd(name: str, cmd: list[str], env: dict[str, str] | None = None) -> int:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    log(f"START {name}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=merged)
    log(f"END   {name}: exit={proc.returncode}")
    return int(proc.returncode)


def fresh_file(path: Path, started_ts: float, min_size: int = 100) -> bool:
    try:
        return path.exists() and path.stat().st_size >= min_size and path.stat().st_mtime >= started_ts
    except Exception:
        return False


def provider_chain(primary: str) -> list[str]:
    primary = (primary or "yfinance").strip().lower()
    raw = os.getenv("KTRADE_SCANNER_FALLBACKS", "").strip()
    if raw:
        chain = [x.strip().lower() for x in raw.split(",") if x.strip()]
    elif primary == "finnhub":
        chain = ["finnhub", "yfinance"]
    else:
        chain = [primary]
    if primary not in chain:
        chain.insert(0, primary)
    # de-duplicate, preserve order
    out: list[str] = []
    for x in chain:
        if x and x not in out:
            out.append(x)
    return out


def run_scanner_once(interval: str, provider: str, universe: str) -> int:
    env = {
        "KTRADE_DATA_PROVIDER": provider,
        "KTRADE_SCAN_SYMBOLS": os.getenv("KTRADE_SCAN_SYMBOLS", ""),
        "KTRADE_SCAN_UNIVERSE": universe,
        "KTRADE_SCAN_INTERVAL": interval,
        "KTRADE_ORDER_SUBMISSION_ENABLED": os.getenv("KTRADE_ORDER_SUBMISSION_ENABLED", "false"),
    }
    return run_cmd(
        f"scanner_{interval}_{provider}",
        [py(), "agent/ktrade_agent_v9.py", "--score-only"],
        env,
    )


def run_scanner_with_fallback(interval: str, primary_provider: str, universe: str) -> int:
    providers = provider_chain(primary_provider)
    last_rc = 1
    for provider in providers:
        started_ts = datetime.now().timestamp()
        log(f"SCANNER_PROVIDER_START provider={provider} interval={interval}")
        rc = run_scanner_once(interval, provider, universe)
        ok_file = fresh_file(SCAN_FILE, started_ts)
        if rc == 0 and ok_file:
            log(f"SCANNER_PROVIDER_OK provider={provider} output={SCAN_FILE}")
            return 0
        log(f"SCANNER_PROVIDER_FAILED provider={provider} rc={rc} fresh_scan_file={ok_file}")
        last_rc = rc or 1
    log(f"SCANNER_ALL_PROVIDERS_FAILED providers={providers}")
    return last_rc


def run_vectorbt_daily() -> int:
    return run_cmd("vectorbt_daily", [py(), "ktrade_vectorbt.py"])


def run_vectorbt_intraday() -> int:
    args = os.getenv("KTRADE_INTRADAY_VECTORBT_ARGS", "--fast --universe extended").split()
    return run_cmd("vectorbt_intraday", [py(), "ktrade_intraday_vectorbt.py", *args])


def should_skip(force: bool) -> bool:
    if force:
        return False
    skip = os.getenv("KTRADE_SKIP_NON_TRADING_DAYS", "true").lower() in {"1", "true", "yes", "y"}
    if not skip:
        return False
    return not is_trading_day(now_et().date())


def run_daily(force: bool = False, no_backtest: bool = False) -> int:
    if should_skip(force):
        log(f"SKIP daily: non-trading day in ET ({now_et().date()})")
        return 0
    provider = os.getenv("KTRADE_DATA_PROVIDER", "yfinance")
    universe = os.getenv("KTRADE_SCAN_UNIVERSE", "extended")
    interval = os.getenv("KTRADE_DAILY_SCAN_INTERVAL", "1d")
    rc1 = run_scanner_with_fallback(interval, provider, universe)
    rc2 = 0 if no_backtest else run_vectorbt_daily()
    return rc1 or rc2


def run_intraday(force: bool = False, no_backtest: bool = False) -> int:
    if should_skip(force):
        log(f"SKIP intraday: non-trading day in ET ({now_et().date()})")
        return 0
    provider = os.getenv("KTRADE_DATA_PROVIDER", "yfinance")
    universe = os.getenv("KTRADE_SCAN_UNIVERSE", "extended")
    interval = os.getenv("KTRADE_INTRADAY_SCAN_INTERVAL", "5m")
    rc1 = run_scanner_with_fallback(interval, provider, universe)
    rc2 = 0 if no_backtest else run_vectorbt_intraday()
    return rc1 or rc2


def status() -> int:
    print("KTrade daily runner status")
    print(f"ROOT={ROOT}")
    print(f"PYTHON={py()}")
    print(f"NOW_ET={now_et().strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"TRADING_DAY={is_trading_day(now_et().date())}")
    print(f"KTRADE_DATA_PROVIDER={os.getenv('KTRADE_DATA_PROVIDER', 'yfinance')}")
    print(f"KTRADE_SCANNER_FALLBACKS={provider_chain(os.getenv('KTRADE_DATA_PROVIDER', 'yfinance'))}")
    for rel in [
        "agent/ktrade_agent_v9.py",
        "ktrade_vectorbt.py",
        "ktrade_intraday_vectorbt.py",
        "data/ktrade_scan_latest.json",
        "data/ktrade_backtest_latest.json",
        "data/ktrade_intraday_backtest_latest.json",
    ]:
        p = ROOT / rel
        print(f"{rel}: {'OK' if p.exists() else 'MISSING'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Run daily/intraday KTrade scanner + VectorBT jobs")
    p.add_argument("mode", choices=["daily", "intraday", "both", "status"], help="Which automation job to run")
    p.add_argument("--force", action="store_true", help="Run even on weekend/holiday")
    p.add_argument("--no-backtest", action="store_true", help="Run scanner only, skip VectorBT")
    args = p.parse_args()

    if args.mode == "status":
        return status()
    if args.mode == "daily":
        return run_daily(force=args.force, no_backtest=args.no_backtest)
    if args.mode == "intraday":
        return run_intraday(force=args.force, no_backtest=args.no_backtest)
    if args.mode == "both":
        return run_intraday(force=args.force, no_backtest=args.no_backtest) or run_daily(force=args.force, no_backtest=args.no_backtest)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
