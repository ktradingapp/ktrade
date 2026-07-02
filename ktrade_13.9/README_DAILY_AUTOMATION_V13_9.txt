KTrade v13.9 Daily Automation Files
===================================

Copy these files into the KTrade repo root, not inside another nested version folder.

Windows usage:
- RUN_FINNHUB_INTRADAY_ALL.cmd          Finnhub 5m intraday scanner + intraday VectorBT
- RUN_FINNHUB_SCANNER_ONLY_INTRADAY.cmd Finnhub 5m intraday scanner only
- RUN_FINNHUB_DAILY_ALL.cmd             Finnhub daily scanner + daily VectorBT
- RUN_DAILY_ALL.cmd                     Default provider daily scanner + daily VectorBT
- RUN_INTRADAY_ALL.cmd                  Default provider intraday scanner + intraday VectorBT

VPS usage:
cd /opt/ktrade_current
source .venv/bin/activate
python ktrade_daily_runner.py status
python ktrade_daily_runner.py intraday --force
python ktrade_daily_runner.py daily --force
bash scripts/install_ktrade_daily_timers.sh /opt/ktrade_current

Recommended VPS setup:
- Keep ktrade-backend.service running for UI/API.
- Keep ktrade-agent.service running for live scanner/trading loop.
- Keep ktrade-scheduler.service running for daily/intraday VectorBT backtests.
- Use these runner files for manual or timer-based scanner/backtest jobs.
