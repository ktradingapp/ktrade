#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${1:-/opt/ktrade_current}"
PY="$APP_DIR/.venv/bin/python"

if [ ! -f "$APP_DIR/ktrade_daily_runner.py" ]; then
  echo "ERROR: $APP_DIR/ktrade_daily_runner.py not found. Copy it into the project first."
  exit 1
fi
if [ ! -x "$PY" ]; then
  echo "ERROR: Python venv not found at $PY"
  exit 1
fi

cat >/etc/systemd/system/ktrade-daily-all.service <<EOF
[Unit]
Description=KTrade Daily Scanner + VectorBT Backtest
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
ExecStart=$PY $APP_DIR/ktrade_daily_runner.py daily
User=root
EOF

cat >/etc/systemd/system/ktrade-intraday-all.service <<EOF
[Unit]
Description=KTrade Intraday Scanner + Intraday VectorBT Backtest
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
ExecStart=$PY $APP_DIR/ktrade_daily_runner.py intraday
User=root
EOF

cat >/etc/systemd/system/ktrade-daily-all.timer <<'EOF'
[Unit]
Description=Run KTrade daily scanner + VectorBT after US close

[Timer]
OnCalendar=Mon..Fri *-*-* 18:10:00 America/New_York
Persistent=true
Unit=ktrade-daily-all.service

[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/ktrade-intraday-all.timer <<'EOF'
[Unit]
Description=Run KTrade intraday scanner + VectorBT before US open

[Timer]
OnCalendar=Mon..Fri *-*-* 08:05:00 America/New_York
Persistent=true
Unit=ktrade-intraday-all.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now ktrade-daily-all.timer ktrade-intraday-all.timer

echo "Installed KTrade daily automation timers."
systemctl list-timers 'ktrade-*all.timer' --no-pager
