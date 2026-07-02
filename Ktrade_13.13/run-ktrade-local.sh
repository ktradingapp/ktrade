#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export KTRADE_BIND_HOST=${KTRADE_BIND_HOST:-127.0.0.1}
export KTRADE_PORT=${KTRADE_PORT:-5001}
export KTRADE_PAPER_ORDER_SUBMISSION=${KTRADE_PAPER_ORDER_SUBMISSION:-false}
export LIVE_TRADING=${LIVE_TRADING:-false}
export KTRADE_MANUAL_ALLOW_DEMO=${KTRADE_MANUAL_ALLOW_DEMO:-false}
python scripts/run_ktrade_local.py --open-browser
