#!/usr/bin/env python3
"""Run KTrade locally with app-data paths, local-only backend, and health checks."""
from ktrade_runtime.supervisor import main

if __name__ == "__main__":
    raise SystemExit(main())
