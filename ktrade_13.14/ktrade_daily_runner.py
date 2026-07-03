#!/usr/bin/env python3
"""
KTrade daily/intraday integrated runner with permanent scanner provider fallback v2.

Fixes an important edge case: agent/ktrade_agent_v9.py can exit with code 0 even
when Finnhub returns HTTP 403 for every symbol and the scan file is unusable.
This runner therefore checks both the process exit code AND scanner output/scan
file quality before accepting a provider as successful.

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
import json
import os
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Any

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


def run_cmd(name: str, cmd: list[str], env: dict[str, str] | None = None, capture: bool = False) -> tuple[int, str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    log(f"START {name}: {' '.join(cmd)}")
    if not capture:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=merged)
        log(f"END   {name}: exit={proc.returncode}")
        return int(proc.returncode), ""

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        print(line, flush=True)
        lines.append(line)
        # Keep a bounded in-memory buffer so huge scans do not consume too much RAM.
        if len(lines) > 5000:
            lines = lines[-3000:]
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    rc = int(proc.wait())
    log(f"END   {name}: exit={rc}")
    return rc, "\n".join(lines)


def fresh_file(path: Path, started_ts: float, min_size: int = 100) -> bool:
    try:
        return path.exists() and path.stat().st_size >= min_size and path.stat().st_mtime >= started_ts
    except Exception:
        return False


def _walk_json(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json(v)


def scan_file_quality(path: Path) -> tuple[bool, str]:
    """Return whether scan file appears usable enough to accept provider output."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"scan_json_invalid:{e}"

    rows = [d for d in _walk_json(obj) if isinstance(d, dict)]
    # Count rows that look like symbol/signal records. Support several possible schemas.
    symbol_rows = []
    usable_rows = []
    error_rows = []
    for d in rows:
        sym = d.get("ticker") or d.get("symbol") or d.get("sym")
        if not sym:
            continue
        symbol_rows.append(d)
        text = json.dumps(d, default=str).lower()
        if "insufficient market-data history" in text or "http 403" in text or "forbidden" in text:
            error_rows.append(d)
            continue
        action = str(d.get("action") or d.get("label") or d.get("side") or "").upper()
        score = d.get("conviction", d.get("score", d.get("rank_score", None)))
        if action in {"BUY", "WATCH", "SELL", "HOLD"} or score is not None:
            usable_rows.append(d)

    if len(symbol_rows) == 0:
        return False, "scan_has_no_symbol_rows"
    if len(error_rows) >= max(5, int(len(symbol_rows) * 0.50)):
        return False, f"scan_mostly_error_rows:{len(error_rows)}/{len(symbol_rows)}"
    if len(usable_rows) == 0:
        return False, f"scan_has_no_usable_signal_rows:{len(symbol_rows)}_symbols"
    return True, f"usable_rows={len(usable_rows)} symbol_rows={len(symbol_rows)} error_rows={len(error_rows)}"


def scanner_output_failed(provider: str, output: str) -> tuple[bool, str]:
    low = output.lower()
    if provider.lower() == "finnhub" and "http 403" in low:
        return True, "finnhub_http_403_detected"
    insufficient_count = low.count("insufficient market-data history")
    if insufficient_count >= 5:
        return True, f"insufficient_history_count={insufficient_count}"
    if "traceback (most recent call last)" in low:
        return True, "python_traceback_detected"
    return False, "output_ok"


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
    out: list[str] = []
    for x in chain:
        if x and x not in out:
            out.append(x)
    return out


def run_scanner_once(interval: str, provider: str, universe: str) -> tuple[int, str]:
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
        capture=True,
    )


def run_scanner_with_fallback(interval: str, primary_provider: str, universe: str) -> int:
    providers = provider_chain(primary_provider)
    last_rc = 1
    for provider in providers:
        started_ts = datetime.now().timestamp()
        log(f"SCANNER_PROVIDER_START provider={provider} interval={interval}")
        rc, output = run_scanner_once(interval, provider, universe)
        ok_file = fresh_file(SCAN_FILE, started_ts)
        out_failed, out_reason = scanner_output_failed(provider, output)
        quality_ok = False
        quality_reason = "scan_file_not_fresh"
        if ok_file:
            quality_ok, quality_reason = scan_file_quality(SCAN_FILE)

        if rc == 0 and ok_file and quality_ok and not out_failed:
            log(f"SCANNER_PROVIDER_OK provider={provider} output={SCAN_FILE} quality={quality_reason}")
            return 0

        log(
            "SCANNER_PROVIDER_FAILED "
            f"provider={provider} rc={rc} fresh_scan_file={ok_file} "
            f"output_failed={out_failed}:{out_reason} quality={quality_ok}:{quality_reason}"
        )
        last_rc = rc or 1
    log(f"SCANNER_ALL_PROVIDERS_FAILED providers={providers}")
    return last_rc


def run_vectorbt_daily() -> int:
    rc, _ = run_cmd("vectorbt_daily", [py(), "ktrade_vectorbt.py"])
    return rc


def run_vectorbt_intraday() -> int:
    args = os.getenv("KTRADE_INTRADAY_VECTORBT_ARGS", "--fast --universe extended").split()
    rc, _ = run_cmd("vectorbt_intraday", [py(), "ktrade_intraday_vectorbt.py", *args])
    return rc


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
    if SCAN_FILE.exists():
        ok, reason = scan_file_quality(SCAN_FILE)
        print(f"data/ktrade_scan_latest.json quality: {'OK' if ok else 'BAD'} ({reason})")
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
