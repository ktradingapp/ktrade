# KTrade — Automatic Backtest Scheduler

Runs the VectorBT backtests automatically, keyed to US market timings:

| Job | When (ET, default) | Script | Purpose |
|-----|--------------------|--------|---------|
| intraday | 08:00 (before open) | ktrade_intraday_vectorbt.py | fresh intraday params |
| daily    | 18:00 (after close) | ktrade_vectorbt.py          | fresh daily params for next session |

Only fires on **trading days** (skips weekends + US holidays), America/New_York
time, at most **once per job per day** (state persisted in
`data/scheduler_state.json`, so restarts don't double-run).

## Pick ONE way to run it

### A) Windows — always-on (Task Scheduler)
1. Double-click `RUN_SCHEDULER.cmd` to test, or
2. Task Scheduler -> Create Task -> Trigger "At startup" -> Action: start
   `RUN_SCHEDULER.cmd`. It self-times and runs forever.

### B) Linux server — cron (recommended for the paper-trade server)
```
crontab deploy/crontab        # edit the /app path first
```
Cron fires at the time; the scheduler still gates on trading days.

### C) Linux server — always-on (systemd)
```
sudo cp deploy/ktrade-scheduler.service /etc/systemd/system/
sudo systemctl enable --now ktrade-scheduler
```

## Configure (optional, via .env)
```
KTRADE_DAILY_BACKTEST_TIME=18:00
KTRADE_INTRADAY_BACKTEST_TIME=08:00
KTRADE_DAILY_BACKTEST_ARGS=
KTRADE_INTRADAY_BACKTEST_ARGS=--fast --universe extended
KTRADE_SCHED_POLL_SECONDS=60
```

## Test / operate
```
python ktrade_scheduler.py --list             # show schedule + last runs
python ktrade_scheduler.py --run-now intraday # force a run now (testing)
python ktrade_scheduler.py --run-if-due daily # run only if due today (cron uses this)
```

## Notes
- The backtests write `data/ktrade_approved_params.json` and
  `data/ktrade_intraday_approved_params.json`; the live agent reads those on its
  next cycle. So scheduling these keeps the agent's params fresh automatically.
- Server timezone: set the box to America/New_York, or shift the cron/`*_TIME`
  values so they land at the ET times you want.
- Holidays are listed in `ktrade_scheduler.py::HOLIDAYS` — extend yearly.
