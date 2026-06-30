# KTrade — Deploy to a Linux VPS (auto-start agent + backend + scheduler)

Goal: after deployment, the agent loop, the Flask backend, and the backtest
scheduler all start on boot and auto-restart on crash — no manual launching.

## 0. Assumptions
- Ubuntu/Debian VPS, deploy path `/app`, service user `ktrade`.
- You run PAPER first (KTRADE_PAPER_ORDER_SUBMISSION decides if orders are placed).

## 1. System setup (once)
```bash
sudo timedatectl set-timezone America/New_York     # so 08:00/18:00 jobs hit ET
sudo adduser --system --group ktrade
sudo mkdir -p /app && sudo chown -R ktrade:ktrade /app
# add 2G swap (4GB/1vCPU box, prevents OOM during a backtest)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Put the code in /app and build the venv
```bash
# copy your ktrade_v11 files into /app (scp / git / rsync), then:
cd /app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo chown -R ktrade:ktrade /app
```

## 3. Create .env (paper keys)
```bash
cp .env.template .env
nano .env
```
Set at minimum:
```
ALPACA_API_KEY=...            # PAPER key
ALPACA_SECRET_KEY=...         # PAPER secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
KTRADE_PAPER_ORDER_SUBMISSION=true
LIVE_TRADING=false
KTRADE_ADMIN_TOKEN=<long-random-string>
FINNHUB_API_KEY=...           # activates the earnings gate
```
(systemd reads .env as KEY=VALUE — no `export`, avoid quotes/spaces around `=`.)

## 4. Install the three services
```bash
sudo cp deploy/ktrade-backend.service   /etc/systemd/system/
sudo cp deploy/ktrade-agent.service     /etc/systemd/system/
sudo cp deploy/ktrade-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
```

## 5. FIRST RUN — verify safely (no orders yet)
Edit the agent unit's ExecStart to append ` --score-only`, then:
```bash
sudo systemctl enable --now ktrade-backend
sudo systemctl enable --now ktrade-agent
sudo systemctl enable --now ktrade-scheduler
systemctl status ktrade-backend ktrade-agent ktrade-scheduler --no-pager
```
Watch logs:
```bash
tail -f /app/ktrade_agent.log
tail -f /app/ktrade_backend.log
.venv/bin/python ktrade_scheduler.py --list
```

## 6. Go to paper trading
Once the loop runs clean and you've done a 1-share paper round-trip:
```bash
sudo nano /etc/systemd/system/ktrade-agent.service   # remove " --score-only"
sudo systemctl daemon-reload
sudo systemctl restart ktrade-agent
```

## Operate
```bash
systemctl status ktrade-agent          # health
journalctl -u ktrade-agent -f          # live logs
sudo systemctl restart ktrade-agent    # restart
sudo systemctl stop ktrade-agent       # emergency stop (halts new trades)
```
All three are `enable`d, so they auto-start on reboot and restart on crash.

## Reminders
- Backend binds 127.0.0.1:5001. To view the dashboard, SSH-tunnel
  (`ssh -L 5001:127.0.0.1:5001 user@vps`) or put nginx + auth in front — do NOT
  bind 0.0.0.0 on a public IP.
- Exit broker hook (PositionMonitor.broker_fn) is still None: bracket stop/target
  legs work at Alpaca, but software-side trailing/earnings exits won't place sells
  until wired. Decide before unattended runs.
