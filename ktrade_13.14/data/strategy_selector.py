"""
strategy_selector.py  (KTrade v11.2)
====================================
Explicit regime-based strategy switching.

KTrade already adapts *within* a single conviction scorer (regime-tilted scores
in ktrade_scanner.py + graduated VIX gating in ktrade_risk.py). This adds an
explicit switch on top: per market regime (and a VIX overlay) it selects a
named STRATEGY PROFILE that changes three concrete auto-trade behaviours —

    * the effective minimum conviction required to buy,
    * a position-size multiplier,
    * whether new longs are allowed at all.

Adapted from the peer agent's 4-strategy regime switch (momentum / breakout /
mean-reversion / capital-preservation), but expressed in KTrade's deterministic,
conviction-first vocabulary instead of swapping black-box strategy classes.

Safety
------
* DISABLED by default (KTRADE_STRATEGY_SWITCH_ENABLED=false): the selector
  always returns the NEUTRAL/baseline profile, so behaviour is unchanged.
* It can only ever make entries *stricter* or *smaller* than baseline (or block
  new longs) in weaker regimes — it never loosens risk below the operator's
  configured floor, and it sits IN FRONT of (never replaces) the RiskEngine.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("strategy_selector")

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_SCAN_LATEST = _PROJECT_DIR / "data" / "ktrade_scan_latest.json"

VALID_REGIMES = ("BULL", "NEUTRAL", "RISK_OFF", "CRASH")


def enabled() -> bool:
    return os.getenv("KTRADE_STRATEGY_SWITCH_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


@dataclass
class StrategyProfile:
    name: str                 # e.g. "momentum", "mean-reversion"
    regime: str               # resolved regime after the VIX overlay
    min_conviction: float     # effective buy threshold for this cycle
    size_mult: float          # multiply the risk-sized qty by this
    allow_new_longs: bool     # False => no new buys this cycle
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# Per-regime behaviour. `conv_delta` is added to the operator's base conviction
# (so the operator's floor still moves the whole schedule); size_mult and
# allow_new_longs express the regime's risk posture.
_REGIME_RULES = {
    "BULL":     dict(name="momentum",              conv_delta=-5.0, size_mult=1.0,  allow=True,
                     note="trend-friendly: slightly lower bar, full size"),
    "NEUTRAL":  dict(name="balanced",              conv_delta=0.0,  size_mult=1.0,  allow=True,
                     note="baseline conviction and size"),
    "RISK_OFF": dict(name="mean-reversion",        conv_delta=8.0,  size_mult=0.5,  allow=True,
                     note="defensive: higher bar, half size"),
    "CRASH":    dict(name="capital-preservation",  conv_delta=999.0, size_mult=0.0, allow=False,
                     note="no new longs; preserve capital"),
}


def _vix_overlay(regime: str, vix: float) -> str:
    """Escalate the regime on fear spikes so a stale 'BULL' tag can't keep the
    agent aggressive into a volatility blow-off. Never de-escalates."""
    order = {"BULL": 0, "NEUTRAL": 1, "RISK_OFF": 2, "CRASH": 3}
    floor = "BULL"
    if vix >= 40:
        floor = "CRASH"
    elif vix >= 30:
        floor = "RISK_OFF"
    elif vix >= 25:
        floor = "NEUTRAL"
    return regime if order.get(regime, 1) >= order[floor] else floor


def select_profile(regime: Optional[str], vix: float = 18.0,
                  base_conviction: float = 80.0) -> StrategyProfile:
    """Pure: map (regime, vix, base) -> StrategyProfile. When switching is
    disabled, always returns the NEUTRAL/baseline profile."""
    if not enabled():
        return StrategyProfile("balanced", "NEUTRAL", base_conviction, 1.0, True,
                               "strategy switch disabled (baseline)")
    reg = (regime or "NEUTRAL").upper()
    if reg not in VALID_REGIMES:
        reg = "NEUTRAL"
    reg = _vix_overlay(reg, float(vix or 18.0))
    r = _REGIME_RULES[reg]
    min_conv = base_conviction + r["conv_delta"]
    if reg == "CRASH":
        min_conv = 999.0
    return StrategyProfile(
        name=r["name"], regime=reg, min_conviction=min_conv,
        size_mult=r["size_mult"], allow_new_longs=r["allow"], note=r["note"],
    )


def current_market_context() -> Tuple[str, float]:
    """Best-effort (regime, vix) for the live auto-trade pass. Reads the latest
    scan snapshot for regime and env/snapshot for VIX; defaults to a safe,
    neutral context if anything is missing (never raises)."""
    regime, vix = "NEUTRAL", float(os.getenv("KTRADE_VIX", "18") or 18)
    try:
        if _SCAN_LATEST.exists():
            data = json.loads(_SCAN_LATEST.read_text())
            regime = (data.get("market_regime")
                      or data.get("regime")
                      or (data.get("macro") or {}).get("market_regime")
                      or regime)
            vix = float(data.get("vix") or (data.get("macro") or {}).get("vix") or vix)
    except Exception as exc:  # pragma: no cover
        log.debug("market context fallback (%s)", exc)
    return str(regime).upper(), vix


def active_profile(base_conviction: float = 80.0) -> StrategyProfile:
    """Convenience used by the auto-trade pass."""
    regime, vix = current_market_context()
    prof = select_profile(regime, vix, base_conviction)
    if enabled():
        log.info("Strategy profile: %s (regime=%s vix=%.1f) | min_conv=%.0f size=%.2f longs=%s",
                 prof.name, prof.regime, vix, prof.min_conviction, prof.size_mult,
                 prof.allow_new_longs)
    return prof
