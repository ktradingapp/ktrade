"""
scan_schema.py - KTrade v10.7
=============================
Validate data/ktrade_scan_latest.json before the dashboard / AI advisor consume
it (#6). Bad JSON, missing keys, string prices, or an old schema can no longer
leak into downstream consumers.
"""
from __future__ import annotations
from typing import Literal, Optional, Dict, List
from datetime import datetime
import json
import logging

log = logging.getLogger("KTrade.scan_schema")

try:
    from pydantic import BaseModel, Field, field_validator
    _HAVE_PYDANTIC = True
except Exception:
    _HAVE_PYDANTIC = False


if _HAVE_PYDANTIC:
    class PriceValidation(BaseModel):
        ok: bool = True
        reason: str = "ok"
        scanner_price: Optional[float] = None
        reference_price: Optional[float] = None
        difference_pct: Optional[float] = None

    class ScanResult(BaseModel):
        ticker: str = Field(..., min_length=1, max_length=16)
        action: Literal["BUY", "WATCH", "SELL"] = "WATCH"
        conviction: float = Field(..., ge=0, le=100)
        price: float = Field(..., gt=0)
        strategy: str = "UNKNOWN"
        atr: float = Field(default=0, ge=0)
        trade_type: Optional[str] = None
        timeframe: Optional[str] = None
        blocked_reason: Optional[str] = None
        price_validation: Optional[PriceValidation] = None
        components: Dict[str, float] = Field(default_factory=dict)

        @field_validator("ticker")
        @classmethod
        def _upper(cls, v):
            return v.strip().upper()

    class ScanPayload(BaseModel):
        generated_at: str
        orders_submitted: bool = False
        universe: str = "core"
        symbols_requested: List[str] = Field(default_factory=list)
        minimum_conviction: float = Field(default=60, ge=0, le=100)
        scan_interval: str = "1d"
        errors: List[str] = Field(default_factory=list)
        results: List[ScanResult] = Field(default_factory=list)


def _empty(reason: str) -> dict:
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "orders_submitted": False,
        "results": [],
        "errors": [f"invalid_scan_json: {reason}"],
    }


def validate_scan_dict(raw: dict) -> dict:
    """Validate a parsed scan dict; return a clean dict or a safe empty payload."""
    if not _HAVE_PYDANTIC:
        # minimal manual checks
        if not isinstance(raw, dict) or not isinstance(raw.get("results", []), list):
            return _empty("not a scan payload")
        return raw
    try:
        return ScanPayload.model_validate(raw).model_dump()
    except Exception as exc:
        log.error("Invalid scan JSON: %s", exc)
        return _empty(str(exc))


def load_valid_scan_payload(path) -> dict:
    """Load + validate a scan JSON file path. Never raises."""
    try:
        from pathlib import Path
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Could not read scan JSON %s: %s", path, exc)
        return _empty(str(exc))
    return validate_scan_dict(raw)
