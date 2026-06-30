"""
portfolio_context.py  (KTrade v11.3 — Milestone 1)
==================================================
KTrade portfolio snapshot feed + portfolio-level kill switches.

KTrade may only see its broker sub-account. An external portfolio snapshot
tracker) is the source of truth for TOTAL capital. This module ingests a signed,
staleness-gated portfolio snapshot and turns it into risk gates that Ktrade
otherwise cannot compute:

    * portfolio-level exposure cap  — size trades as a fraction of TOTAL net
      worth, not the trading sub-account (prevents over-concentration Ktrade
      currently can't even see);
    * net-worth drawdown breaker    — halt new longs if TOTAL net worth (all
      assets) falls past a threshold from its peak;
    * data-staleness breaker        — a stale/forged feed fails SAFE (no new
      risk), never to "size up".

Direction is strictly one-way: Portfolio writes a snapshot, Ktrade reads it. Ktrade
never writes to Portfolio and Portfolio never sends orders. That keeps each system's
blast radius contained.

Snapshot format example:
    {
      "net_worth": 540000.0,
      "liquid": 180000.0,
      "committed": 360000.0,
      "trading_allocation": 100000.0,
      "as_of": "2026-06-27T14:00:00+00:00",
      "sig": "<hex hmac-sha256 of the canonical body>"   # optional
    }

Safety
------
* DISABLED by default (KTRADE_PORTFOLIO_GATE_ENABLED=false) -> all gates allow,
  behaviour unchanged.
* When enabled, a missing/stale/implausible/forged snapshot fails to the
  configured mode (default "block": no new entries) — risk-first.
* A bad net-worth value is caught by a price_sanity-style jump guard before it
  can drive sizing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("portfolio_context")

_PROJECT_DIR = Path(__file__).resolve().parent.parent


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _b("KTRADE_PORTFOLIO_GATE_ENABLED", "false")


def _cfg() -> dict:
    return {
        "snapshot":       os.getenv("KTRADE_PORTFOLIO_SNAPSHOT",
                                    str(_PROJECT_DIR / "data" / "portfolio_snapshot.json")),
        "state":          os.getenv("KTRADE_PORTFOLIO_STATE",
                                    str(_PROJECT_DIR / "data" / "portfolio_state.json")),
        "hmac_secret":    os.getenv("KTRADE_PORTFOLIO_HMAC_SECRET", ""),
        "max_stale_min":  float(os.getenv("KTRADE_PORTFOLIO_MAX_STALE_MIN", "60")),
        "max_position":   float(os.getenv("KTRADE_PORTFOLIO_MAX_POSITION_PCT", "5")),
        "max_deployed":   float(os.getenv("KTRADE_PORTFOLIO_MAX_DEPLOYED_PCT", "30")),
        "dd_halt":        float(os.getenv("KTRADE_NETWORTH_DD_HALT_PCT", "15")),
        "max_jump":       float(os.getenv("KTRADE_PORTFOLIO_MAX_JUMP_PCT", "35")),
        "fail_mode":      os.getenv("KTRADE_PORTFOLIO_FAIL_MODE", "block").strip().lower(),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(s))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _canonical(body: dict) -> str:
    """Deterministic JSON of the snapshot body (excluding the signature) so the
    HMAC is reproducible on both sides."""
    return json.dumps({k: body[k] for k in sorted(body) if k != "sig"},
                      separators=(",", ":"), sort_keys=True)


@dataclass
class PortfolioContext:
    net_worth: float
    liquid: float
    committed: float
    trading_allocation: float
    as_of: datetime
    peak_net_worth: float
    stale: bool

    @property
    def drawdown_pct(self) -> float:
        if self.peak_net_worth <= 0:
            return 0.0
        return max(0.0, (self.peak_net_worth - self.net_worth) / self.peak_net_worth * 100.0)


class PortfolioGate:
    def __init__(self):
        self._lock = threading.RLock()

    # ── snapshot ingest ──────────────────────────────────────────────────────
    def _read_state(self) -> dict:
        try:
            return json.loads(Path(_cfg()["state"]).read_text())
        except Exception:
            return {}

    def _write_state(self, state: dict) -> None:
        try:
            p = Path(_cfg()["state"])
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            os.replace(tmp, p)
        except Exception as exc:  # pragma: no cover
            log.warning("portfolio state write failed: %s", exc)

    def _verify_sig(self, body: dict) -> bool:
        secret = _cfg()["hmac_secret"]
        if not secret:
            return True  # signing not configured -> accept (logged once by caller)
        want = body.get("sig", "")
        got = hmac.new(secret.encode(), _canonical(body).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(str(want), got)

    def get_context(self) -> Optional[PortfolioContext]:
        """Load + validate the latest snapshot. Returns None if missing/forged/
        implausible. Sets `stale=True` (but still returns) if merely old, so the
        caller can decide. Never raises."""
        cfg = _cfg()
        try:
            raw = json.loads(Path(cfg["snapshot"]).read_text())
        except Exception:
            return None
        try:
            if not self._verify_sig(raw):
                log.warning("[PORTFOLIO] snapshot signature mismatch -> rejected")
                return None
            nw = float(raw.get("net_worth", 0) or 0)
            if nw <= 0:
                log.warning("[PORTFOLIO] non-positive net_worth -> rejected")
                return None

            with self._lock:
                state = self._read_state()
                last_good = float(state.get("last_good_net_worth", 0) or 0)
                # price_sanity-style jump guard: an implausible swing vs the last
                # good value is treated as bad data, not a real move.
                if last_good > 0:
                    jump = abs(nw - last_good) / last_good * 100.0
                    if jump > cfg["max_jump"]:
                        log.warning("[PORTFOLIO] net_worth jump %.0f%% > %.0f%% -> rejected as bad data",
                                    jump, cfg["max_jump"])
                        return None
                peak = max(float(state.get("peak_net_worth", 0) or 0), nw)
                state.update({"last_good_net_worth": nw, "peak_net_worth": peak,
                              "updated_at": _now().isoformat()})
                self._write_state(state)

            as_of = _parse_iso(raw.get("as_of")) or _now()
            age_min = (_now() - as_of).total_seconds() / 60.0
            stale = age_min > cfg["max_stale_min"]
            return PortfolioContext(
                net_worth=nw,
                liquid=float(raw.get("liquid", 0) or 0),
                committed=float(raw.get("committed", 0) or 0),
                trading_allocation=float(raw.get("trading_allocation", 0) or 0),
                as_of=as_of, peak_net_worth=peak, stale=stale,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("[PORTFOLIO] snapshot parse error -> rejected: %s", exc)
            return None

    # ── gates ────────────────────────────────────────────────────────────────
    def _fail(self, reason: str) -> Tuple[bool, str]:
        """Apply the configured fail mode when context is unavailable/stale."""
        if _cfg()["fail_mode"] == "allow":
            log.warning("[PORTFOLIO] %s -> fail-mode=allow (permitting)", reason)
            return True, f"{reason} (fail-open)"
        return False, f"{reason} (failing safe)"

    def cycle_gate(self) -> Tuple[bool, str]:
        """Cycle-level kill switch: net-worth drawdown + staleness. Call once per
        auto-trade pass; if it returns False, skip the whole cycle."""
        if not enabled():
            return True, "portfolio gate disabled"
        ctx = self.get_context()
        if ctx is None:
            return self._fail("portfolio snapshot missing/invalid")
        if ctx.stale:
            return self._fail("portfolio snapshot stale")
        dd = ctx.drawdown_pct
        if dd >= _cfg()["dd_halt"]:
            return False, f"net-worth drawdown {dd:.1f}% >= halt {_cfg()['dd_halt']:.0f}%"
        return True, f"ok (net worth ${ctx.net_worth:,.0f}, dd {dd:.1f}%)"

    def exposure_ok(self, intended_notional: float, deployed_notional: float) -> Tuple[bool, str]:
        """Per-trade portfolio exposure check against TOTAL net worth."""
        if not enabled():
            return True, "portfolio gate disabled"
        ctx = self.get_context()
        if ctx is None or ctx.stale:
            return self._fail("portfolio snapshot missing/stale for exposure check")
        nw = ctx.net_worth
        cfg = _cfg()
        pos_cap = cfg["max_position"] / 100.0 * nw
        if intended_notional > pos_cap:
            return False, (f"position ${intended_notional:,.0f} > {cfg['max_position']:.0f}% "
                           f"of net worth (${pos_cap:,.0f})")
        dep_cap = cfg["max_deployed"] / 100.0 * nw
        if deployed_notional + intended_notional > dep_cap:
            return False, (f"deployed ${deployed_notional + intended_notional:,.0f} > "
                           f"{cfg['max_deployed']:.0f}% of net worth (${dep_cap:,.0f})")
        return True, "ok"

    def status(self) -> dict:
        ctx = self.get_context()
        if ctx is None:
            return {"enabled": enabled(), "available": False}
        return {
            "enabled": enabled(), "available": True, "stale": ctx.stale,
            "net_worth": round(ctx.net_worth, 2), "peak": round(ctx.peak_net_worth, 2),
            "drawdown_pct": round(ctx.drawdown_pct, 2),
            "as_of": ctx.as_of.isoformat(),
        }


_GATE: Optional[PortfolioGate] = None
_GATE_LOCK = threading.Lock()


def get_portfolio() -> PortfolioGate:
    global _GATE
    if _GATE is None:
        with _GATE_LOCK:
            if _GATE is None:
                _GATE = PortfolioGate()
    return _GATE
