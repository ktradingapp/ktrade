# KTrade ChatGPT V12 — Manual Execution + Autonomous Mode Completion

This patch finishes the remaining cleanup called out in the KTrade-only V12 review.

## Completed

- Manual dashboard BUY now uses strict schema validation, the shared RiskEngine, and Alpaca bracket orders.
- Manual dashboard BUY refuses to self-seed price sanity from the current broker snapshot. It requires a trusted scanner/reference price.
- Manual dashboard SELL uses strict schema validation and the shared RiskEngine, and refuses accidental shorts unless shorts are enabled.
- Manual order endpoints refuse offline/demo execution unless `KTRADE_MANUAL_ALLOW_DEMO=true`.
- Backend manual risk gate now uses one process-wide RiskEngine, preserving cooldowns, duplicate windows, daily counters, and persisted state.
- No-flag autonomous mode now uses the same default market-data loader as `--once` instead of running empty cycles.
- Autonomous cycles are wrapped in fail-safe exception handling.
- Parallel score-only worker failures are caught per-symbol instead of crashing the entire scan.

## Still keep these safe defaults until your 1-share paper round trip passes

```env
KTRADE_PAPER_ORDER_SUBMISSION=false
LIVE_TRADING=false
KTRADE_MANUAL_ALLOW_DEMO=false
```

## Recommended first run

```bash
python agent/ktrade_agent_v9.py --score-only
python agent/ktrade_agent_v9.py --once
```

Only after scanner output includes valid `price_validation.reference_price` values should dashboard manual BUY be used.
