# KTrade v10.4 Fixed Package

Applied fixes:

1. Removed `.env`, `.venv`, logs, `__pycache__`, and compiled files from the distributable package.
2. Added Alpaca reference-price validation to score-only scans. BUY is downgraded to WATCH if scanner price differs from Alpaca by more than `KTRADE_MAX_PRICE_DIFF_PCT` (default 5%).
3. Wired approved daily VectorBT params into live MACD, EMA trend, and momentum confirmation logic.
4. Wired approved intraday params into ORB/ORB+VWAP/VWAP reclaim/trend-continuation confirmation logic.
5. Fixed intraday VectorBT same-bar look-ahead by shifting entries/exits one bar before portfolio simulation.
6. Moved AI Advisor calls from browser-side Anthropic API usage to backend `/ai/advisor`; put `ANTHROPIC_API_KEY` in backend `.env`.
7. Added demo-signal guard metadata so demo-only bundles are display-only and not tradable.
8. Expanded `.gitignore` to prevent secrets/runtime files from being repackaged.

After copying files, create your own local `.env` from `.env.template`; do not upload or share real keys.
