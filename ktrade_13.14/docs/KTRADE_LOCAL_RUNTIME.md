# KTrade V12.2 Local Runtime Merge

This update adds the best *desktop-app runtime logic* into KTrade without adding any external-app-branded files or app-specific code.

## What was added

- `ktrade_runtime/paths.py` — keeps mutable runtime files outside the source tree.
- `ktrade_runtime/supervisor.py` — starts the KTrade backend locally with health checks and graceful shutdown.
- `ktrade_runtime/process_lock.py` — prevents duplicate local backend instances.
- `scripts/run_ktrade_local.py` — local runner for the supervised backend.
- `/runtime/status` backend endpoint — shows the active app-data/log/db paths.
- `RUN_KTRADE_LOCAL.cmd` and `run-ktrade-local.sh` — one-command local launchers.

## Why this is useful

The trading/risk logic remains KTrade-only. The runtime logic improves operations:

- Source folder stays clean and shareable.
- `.env`, logs, and SQLite DB can live in an OS app-data folder.
- Backend defaults remain local-only: `127.0.0.1`.
- Manual/demo/live order submission remains disabled by default.
- macOS can optionally prevent sleep while the supervised backend is running.
- Duplicate local backend starts are blocked by a lock file.

## Runtime paths

Default runtime directory:

- macOS: `~/Library/Application Support/KTrade`
- Windows: `%LOCALAPPDATA%\\KTrade`
- Linux: `~/.local/share/ktrade`

Override with:

```bash
export KTRADE_APP_DATA_DIR=/custom/path
```

Useful env vars:

```env
KTRADE_APP_DATA_DIR=
KTRADE_ENV_FILE=
KTRADE_LOG_DIR=
KTRADE_DB_PATH=
KTRADE_BIND_HOST=127.0.0.1
KTRADE_PORT=5001
KTRADE_PREVENT_SLEEP=true
KTRADE_PAPER_ORDER_SUBMISSION=false
LIVE_TRADING=false
KTRADE_MANUAL_ALLOW_DEMO=false
```

## Run locally

macOS/Linux:

```bash
./run-ktrade-local.sh
```

Windows:

```cmd
RUN_KTRADE_LOCAL.cmd
```

Or directly:

```bash
python scripts/run_ktrade_local.py --open-browser
```

Then check:

```text
http://127.0.0.1:5001/health
http://127.0.0.1:5001/runtime/status
```

## Safety notes

This runtime merge does not make the app live-trading ready by itself. Keep these defaults until paper tests pass:

```env
KTRADE_PAPER_ORDER_SUBMISSION=false
LIVE_TRADING=false
KTRADE_MANUAL_ALLOW_DEMO=false
```

Recommended paper workflow:

```bash
python agent/ktrade_agent_v9.py --score-only
python agent/ktrade_agent_v9.py --once
python scripts/run_ktrade_local.py --open-browser
```

Then test one-symbol / one-share paper bracket order only after scanner output has trusted price references.
