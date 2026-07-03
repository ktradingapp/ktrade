#!/usr/bin/env python3
"""
ktrade_scheduler.py - KTrade backtest scheduler
===============================================
Runs the VectorBT backtests automatically, keyed to US market timings:

  * DAILY backtest    -> after the close (default 18:00 ET)  -> fresh approved
    params for the next session.
  * INTRADAY backtest -> before the open (default 08:00 ET).

Only fires on TRADING DAYS (skips weekends + US market holidays), in
America/New_York time. Runs each job at most once per trading day (state
persisted, so a restart won't double-run).

Two ways to use it:
  1. Always-on:   python ktrade_scheduler.py
     (a lightweight loop that self-times; good for Windows Task Scheduler "at
      startup" or a long-running server process / Docker.)
  2. Cron-driven: cron fires at the time, the scheduler does the trading-day gate:
        0 18 * * 1-5  cd /app && python ktrade_scheduler.py --run-if-due daily
        0 8  * * 1-5  cd /app && python ktrade_scheduler.py --run-if-due intraday

Manual / testing:
  python ktrade_scheduler.py --list                 # show schedule + last runs
  python ktrade_scheduler.py --run-now daily         # force-run now (ignores gate)
  python ktrade_scheduler.py --run-if-due intraday   # run only if due today

Config via env (.env):
  KTRADE_DAILY_BACKTEST_TIME     (default "18:00")
  KTRADE_INTRADAY_BACKTEST_TIME  (default "08:00")
  KTRADE_DAILY_BACKTEST_ARGS     (default "")
  KTRADE_INTRADAY_BACKTEST_ARGS  (default "--fast --universe extended")
  KTRADE_SCHED_POLL_SECONDS      (default "60")
  KTRADE_SCHED_CATCHUP           (default "false")  # if started after the time,
                                                    # run today's missed job once
"""
from __future__ import annotations
import os
import sys
import json
import time
import argparse
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "data" / "scheduler_state.json"
LOG_FILE = HERE / "ktrade_scheduler.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SCHED] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, mode="a")],
)
log = logging.getLogger("KTrade.scheduler")

# US market holidays (mirror of HeartbeatEngine; extend yearly).
HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()  # last-resort fallback


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:                       # Sat/Sun
        return False
    return d.strftime("%Y-%m-%d") not in HOLIDAYS


def _parse_hhmm(s: str, default=(0, 0)):
    try:
        h, m = str(s).strip().split(":")
        return int(h), int(m)
    except Exception:
        return default


@dataclass
class Job:
    name: str
    hour: int
    minute: int
    script: str
    args: list

    def minutes(self) -> int:
        return self.hour * 60 + self.minute


def build_jobs() -> dict:
    dh, dm = _parse_hhmm(os.getenv("KTRADE_DAILY_BACKTEST_TIME", "18:00"), (18, 0))
    ih, im = _parse_hhmm(os.getenv("KTRADE_INTRADAY_BACKTEST_TIME", "08:00"), (8, 0))
    daily_args = os.getenv("KTRADE_DAILY_BACKTEST_ARGS", "").split()
    intra_args = os.getenv("KTRADE_INTRADAY_BACKTEST_ARGS", "--fast --universe extended").split()
    return {
        "daily": Job("daily", dh, dm, "ktrade_vectorbt.py", daily_args),
        "intraday": Job("intraday", ih, im, "ktrade_intraday_vectorbt.py", intra_args),
    }


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(state: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:
        log.warning("Could not save scheduler state: %s", exc)


def is_due(job: Job, when: datetime, last_run_iso: str | None, catchup: bool = False) -> bool:
    """Pure decision: should `job` run at `when`, given its last run date?
    Due when: it's a trading day, we've reached the scheduled minute, and it
    hasn't already run today. With catchup=False we still allow any time at/after
    the scheduled minute on the same day (so a 60s poll won't miss it)."""
    d = when.date()
    if not is_trading_day(d):
        return False
    if last_run_iso == d.isoformat():
        return False
    reached = (when.hour * 60 + when.minute) >= job.minutes()
    return reached


def run_job(job: Job) -> bool:
    """Run a backtest script as a subprocess. Returns True on exit code 0."""
    py = sys.executable or "python"
    script = HERE / job.script
    if not script.exists():
        log.error("Job %s: script not found: %s", job.name, script)
        return False
    cmd = [py, str(script), *job.args]
    log.info("Job %s starting: %s", job.name, " ".join(cmd))
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True, timeout=3600)
        dt = time.time() - t0
        if proc.returncode == 0:
            log.info("Job %s OK in %.0fs", job.name, dt)
            if proc.stdout:
                log.info("Job %s tail: %s", job.name, proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "")
            return True
        log.error("Job %s FAILED (exit %s) in %.0fs: %s",
                  job.name, proc.returncode, dt, (proc.stderr or "")[-400:])
        return False
    except subprocess.TimeoutExpired:
        log.error("Job %s TIMED OUT after 1h", job.name)
        return False
    except Exception as exc:
        log.error("Job %s crashed: %s", job.name, exc)
        return False


def mark_ran(job: Job, when: datetime):
    state = load_state()
    state[job.name] = when.date().isoformat()
    save_state(state)


def run_if_due(name: str) -> int:
    jobs = build_jobs()
    job = jobs.get(name)
    if not job:
        log.error("Unknown job: %s", name)
        return 2
    when = now_et()
    state = load_state()
    if not is_due(job, when, state.get(job.name)):
        log.info("Job %s not due (trading_day=%s, last_run=%s, now=%s)",
                 name, is_trading_day(when.date()), state.get(job.name), when.strftime("%Y-%m-%d %H:%M %Z"))
        return 0
    ok = run_job(job)
    if ok:
        mark_ran(job, when)
    return 0 if ok else 1


def run_now(name: str) -> int:
    jobs = build_jobs()
    job = jobs.get(name)
    if not job:
        log.error("Unknown job: %s", name)
        return 2
    ok = run_job(job)
    if ok:
        mark_ran(job, now_et())
    return 0 if ok else 1


def list_schedule():
    jobs = build_jobs()
    state = load_state()
    when = now_et()
    print(f"Now: {when.strftime('%Y-%m-%d %H:%M %Z')}  trading_day={is_trading_day(when.date())}")
    for j in jobs.values():
        print(f"  {j.name:9s} {j.hour:02d}:{j.minute:02d} ET  ->  {j.script} {' '.join(j.args)}"
              f"   last_run={state.get(j.name, 'never')}")


def loop(poll_seconds: int = 60, catchup: bool = False):
    jobs = build_jobs()
    log.info("Scheduler started. Poll=%ss  catchup=%s", poll_seconds, catchup)
    list_schedule()
    while True:
        when = now_et()
        state = load_state()
        for job in jobs.values():
            try:
                if is_due(job, when, state.get(job.name), catchup=catchup):
                    if run_job(job):
                        mark_ran(job, when)
            except Exception as exc:
                log.error("scheduler tick error on %s: %s", job.name, exc)
        time.sleep(max(10, poll_seconds))


def main():
    p = argparse.ArgumentParser(description="KTrade backtest scheduler")
    p.add_argument("--list", action="store_true", help="Show schedule + last runs")
    p.add_argument("--run-now", metavar="JOB", help="Force-run a job now (daily|intraday)")
    p.add_argument("--run-if-due", metavar="JOB", help="Run a job only if due today (for cron)")
    args = p.parse_args()

    if args.list:
        list_schedule(); return 0
    if args.run_now:
        return run_now(args.run_now)
    if args.run_if_due:
        return run_if_due(args.run_if_due)
    # default: always-on loop
    poll = int(os.getenv("KTRADE_SCHED_POLL_SECONDS", "60"))
    catchup = os.getenv("KTRADE_SCHED_CATCHUP", "false").lower() == "true"
    loop(poll, catchup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
