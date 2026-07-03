"""
KTrade PRO — Risk Management Engine
=====================================
The "Logic Wrapper" that sits BETWEEN the AI and the broker.
The AI makes predictions. This module decides if it's SAFE to act on them.

Architecture:
  AI Signal → Risk Engine → [APPROVED / BLOCKED] → Broker (Alpaca)

Rules of this module:
  1. ALL risk checks are deterministic math — no ML, no AI
  2. If ANY check fails → trade is BLOCKED, reason logged
  3. Kill switch overrides EVERYTHING including the AI
  4. Broker-side bracket orders fire the moment a trade is placed

INSTALL:
  pip install requests pandas numpy alpaca-trade-api

USAGE:
  from ktrade_risk import RiskEngine
  engine = RiskEngine(account_equity=100000)
  result = engine.evaluate(ticker="NVDA", side="buy", qty=5, price=131.20)
  if result.approved:
      place_order(...)
"""

import time
import logging
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("KTrade.Risk")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [RISK] %(message)s", datefmt="%H:%M:%S")

# ─────────────────────────────────────────────────────────────────────────────
# RISK CONFIGURATION  ← edit these values to match your risk tolerance
# ─────────────────────────────────────────────────────────────────────────────
class RiskConfig:
    # ── Kill Switch ───────────────────────────────────────────────────────────
    KILL_SWITCH_ACTIVE       = False      # manually flip True to halt ALL trading
    MAX_DAILY_DRAWDOWN_PCT   = 3.0        # halt if account drops 3% in one day
    MAX_DAILY_LOSS_DOLLARS   = 3000       # absolute dollar cap per day
    MAX_OPEN_POSITIONS       = 10         # never hold more than N positions

    # ── Per-trade limits ──────────────────────────────────────────────────────
    MAX_POSITION_SIZE_PCT    = 10.0       # no single position > 10% of account
    MAX_TRADE_DOLLAR_RISK    = 1000       # max dollar risk per trade (stop × qty)
    MIN_CONVICTION_SCORE     = 75         # reject signals below this conviction

    # ── Volatility / ATR sizing ───────────────────────────────────────────────
    ATR_RISK_MULTIPLIER      = 1.5        # stop = entry - (ATR × multiplier)
    HIGH_VOL_REDUCTION_PCT   = 50         # if VIX > HIGH_VIX, cut position size 50%

    # ── VIX Circuit Breakers ──────────────────────────────────────────────────
    VIX_RISK_OFF_THRESHOLD   = 30.0       # pause new longs if VIX > 30
    VIX_CLOSE_ALL_THRESHOLD  = 50.0       # close ALL positions if VIX > 50
    HIGH_VIX                 = 25.0       # reduce sizing if VIX > 25

    # ── Flash Crash Detection ─────────────────────────────────────────────────
    FLASH_CRASH_DROP_PCT     = 2.5        # if SPY drops 2.5% in < 10 min → halt
    MAX_BID_ASK_SPREAD_PCT   = 0.5        # options spread > 0.5% of price → skip
    MIN_OPTION_VOLUME        = 100        # reject options with < 100 daily volume

    # ── Bracket Order Defaults ────────────────────────────────────────────────
    DEFAULT_STOP_PCT         = 2.0        # default stop-loss: 2% below entry
    DEFAULT_TARGET_PCT       = 4.0        # default take-profit: 4% above entry
    TRAILING_STOP_PCT        = 1.5        # trailing stop: 1.5% from high

    # ── Kelly Criterion ───────────────────────────────────────────────────────
    KELLY_WIN_RATE           = 0.55       # historical win rate (update over time)
    KELLY_AVG_WIN            = 1.8        # average win / average loss ratio
    KELLY_FRACTION           = 0.25       # use 25% of full Kelly (conservative)

    # ── FIX 1: Duplicate Order Prevention ────────────────────────────────────
    DUPLICATE_WINDOW_SECONDS = 60         # block same ticker+side within 60 seconds
    MAX_SAME_TICKER_PER_DAY  = 3          # max entries in same ticker per day

    # ── FIX 2: Overexposure / Same-Ticker Cooldown ───────────────────────────
    TICKER_COOLDOWN_SECONDS  = 300        # 5 min cooldown after any fill on same ticker
    MAX_TICKER_EXPOSURE_PCT  = 20.0       # total exposure in one ticker <= 20% of account

    # ── FIX 3: Hedge / Short Allowance ───────────────────────────────────────
    ALLOW_SHORTS             = True       # allow short sells as hedge
    AUTO_HEDGE_VIX           = 28.0       # auto-suggest hedge when VIX > 28
    MAX_SHORT_EXPOSURE_PCT   = 15.0       # total short exposure cap


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TradeRequest:
    ticker:      str
    side:        str          # "buy" or "sell"
    qty:         float
    price:       float
    conviction:  int   = 80
    strategy:    str   = ""
    option_type: str   = ""   # "CALL", "PUT", or "" for stock
    spread:      float = 0.0  # bid-ask spread (options)
    atr:         float = 0.0  # Average True Range
    iv:          float = 0.0  # Implied Volatility (0-1)

@dataclass
class RiskDecision:
    approved:    bool
    reason:      str
    ticker:      str
    side:        str
    original_qty: float
    approved_qty: float        = 0.0
    stop_price:  float         = 0.0
    target_price: float        = 0.0
    dollar_risk: float         = 0.0
    kelly_size:  float         = 0.0
    warnings:    list          = field(default_factory=list)
    timestamp:   str           = field(default_factory=lambda: datetime.now().isoformat())

@dataclass
class MarketState:
    vix:               float = 18.0
    spy_price:         float = 550.0
    spy_5min_change:   float = 0.0    # % change in last 5 minutes
    spy_10min_change:  float = 0.0    # % change in last 10 minutes
    market_open:       bool  = True
    flash_crash_active: bool = False
    last_updated:      str   = ""


# ─────────────────────────────────────────────────────────────────────────────
# RISK ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class RiskEngine:
    def __init__(self, account_equity: float, config: RiskConfig = None):
        self.equity          = account_equity
        self.equity_open     = account_equity   # equity at market open today
        self.cfg             = config or RiskConfig()
        self.market          = MarketState()
        self.daily_pnl       = 0.0
        self.open_positions  = {}               # ticker → position info
        self.trade_log       = []
        self.kill_active     = False
        self.blocked_today   = 0
        self.approved_today  = 0

        # FIX 1 — Duplicate order prevention
        self.last_order_time  = {}   # "TICKER_side" -> datetime of last order
        self.ticker_day_count = {}   # "TICKER" -> int count of orders today

        # FIX 2 — Per-ticker cooldown after fills
        self.last_fill_time   = {}   # "TICKER" -> datetime of last confirmed fill

        # FIX 3 — Short/hedge exposure tracking
        self.short_positions  = {}   # ticker -> short dollar exposure

        log.info(f"Risk Engine initialized | Equity: ${equity:,.0f} | MDD: {self.cfg.MAX_DAILY_DRAWDOWN_PCT}% | VIX halt: {self.cfg.VIX_RISK_OFF_THRESHOLD}")

    # ── Main evaluation entry point ───────────────────────────────────────────
    def evaluate(self, trade: TradeRequest) -> RiskDecision:
        """
        Run ALL risk checks against a proposed trade.
        Returns RiskDecision with approved=True only if ALL checks pass.
        """
        warnings = []
        log.info(f"Evaluating: {trade.side.upper()} {trade.qty}x {trade.ticker} @ ${trade.price:.2f} | CV:{trade.conviction}")

        # ── CHECK 1: Manual kill switch ───────────────────────────────────────
        if self.cfg.KILL_SWITCH_ACTIVE or self.kill_active:
            return self._block(trade, "KILL SWITCH ACTIVE — all trading halted", warnings)

        # ── CHECK 2: Market hours / flash crash ───────────────────────────────
        if self.market.flash_crash_active:
            return self._block(trade, f"FLASH CRASH DETECTED — SPY {self.market.spy_10min_change:.1f}% in 10min — trading halted", warnings)

        # ── CHECK 3: VIX circuit breaker ─────────────────────────────────────
        if self.market.vix >= self.cfg.VIX_CLOSE_ALL_THRESHOLD:
            self._trigger_close_all()
            return self._block(trade, f"VIX EMERGENCY ({self.market.vix:.1f}) — close all mode, no new positions", warnings)

        if self.market.vix >= self.cfg.VIX_RISK_OFF_THRESHOLD and trade.side == "buy":
            return self._block(trade, f"VIX RISK-OFF ({self.market.vix:.1f} > {self.cfg.VIX_RISK_OFF_THRESHOLD}) — no new longs", warnings)

        if self.market.vix >= self.cfg.HIGH_VIX:
            warnings.append(f"Elevated VIX ({self.market.vix:.1f}) — position size reduced 50%")

        # ── CHECK 4: Max daily drawdown ───────────────────────────────────────
        drawdown_pct = (self.equity_open - self.equity) / self.equity_open * 100
        if drawdown_pct >= self.cfg.MAX_DAILY_DRAWDOWN_PCT:
            self.kill_active = True
            return self._block(trade, f"MAX DAILY DRAWDOWN HIT ({drawdown_pct:.1f}%) — AI keys revoked for today", warnings)

        if abs(self.daily_pnl) >= self.cfg.MAX_DAILY_LOSS_DOLLARS:
            self.kill_active = True
            return self._block(trade, f"DAILY LOSS LIMIT HIT (${abs(self.daily_pnl):,.0f}) — halting for today", warnings)

        # ── CHECK 5: Conviction gate ──────────────────────────────────────────
        if trade.conviction < self.cfg.MIN_CONVICTION_SCORE:
            return self._block(trade, f"LOW CONVICTION ({trade.conviction} < {self.cfg.MIN_CONVICTION_SCORE}) — signal rejected", warnings)

        # ── CHECK 6a: FIX 1 — Duplicate order prevention ────────────────────
        order_key = f"{trade.ticker}_{trade.side}"
        now = datetime.now()
        if order_key in self.last_order_time:
            elapsed = (now - self.last_order_time[order_key]).total_seconds()
            if elapsed < self.cfg.DUPLICATE_WINDOW_SECONDS:
                return self._block(trade,
                    f"DUPLICATE ORDER BLOCKED — {trade.ticker} {trade.side} already sent {elapsed:.0f}s ago (window={self.cfg.DUPLICATE_WINDOW_SECONDS}s). "
                    f"This prevents the LITE x4 same-timestamp bug.", warnings)

        ticker_count = self.ticker_day_count.get(trade.ticker, 0)
        if ticker_count >= self.cfg.MAX_SAME_TICKER_PER_DAY:
            return self._block(trade,
                f"TICKER DAY LIMIT — {trade.ticker} already traded {ticker_count}x today (max={self.cfg.MAX_SAME_TICKER_PER_DAY})", warnings)

        # ── CHECK 6b: FIX 2 — Same-ticker cooldown after fill ────────────────
        if trade.ticker in self.last_fill_time:
            since_fill = (now - self.last_fill_time[trade.ticker]).total_seconds()
            if since_fill < self.cfg.TICKER_COOLDOWN_SECONDS:
                remaining = int(self.cfg.TICKER_COOLDOWN_SECONDS - since_fill)
                return self._block(trade,
                    f"COOLDOWN ACTIVE — {trade.ticker} last filled {since_fill:.0f}s ago. "
                    f"Wait {remaining}s before re-entering. Prevents APLD double-fire bug.", warnings)

        # ── CHECK 6c: FIX 3 — Short/hedge checks ─────────────────────────────
        if trade.side == "sell" and trade.ticker not in self.open_positions:
            # This is a short, not closing a long
            if not self.cfg.ALLOW_SHORTS:
                return self._block(trade, "SHORT SELLING disabled in config", warnings)
            total_short_exposure = sum(self.short_positions.values())
            new_short_val = trade.qty * trade.price
            if (total_short_exposure + new_short_val) / self.equity * 100 > self.cfg.MAX_SHORT_EXPOSURE_PCT:
                return self._block(trade,
                    f"SHORT EXPOSURE CAP — adding ${new_short_val:.0f} would exceed {self.cfg.MAX_SHORT_EXPOSURE_PCT}% limit", warnings)

        if self.market.vix >= self.cfg.AUTO_HEDGE_VIX and trade.side == "buy":
            warnings.append(f"VIX={self.market.vix:.1f} > {self.cfg.AUTO_HEDGE_VIX} — consider adding PUT hedge on this position")

        # ── CHECK 6: Max open positions ───────────────────────────────────────
        if len(self.open_positions) >= self.cfg.MAX_OPEN_POSITIONS and trade.side == "buy":
            return self._block(trade, f"MAX POSITIONS ({self.cfg.MAX_OPEN_POSITIONS}) reached — close something first", warnings)

        # ── CHECK 7: Options liquidity check ─────────────────────────────────
        if trade.option_type and trade.spread > 0:
            spread_pct = trade.spread / trade.price * 100
            if spread_pct > self.cfg.MAX_BID_ASK_SPREAD_PCT:
                return self._block(trade, f"OPTIONS SPREAD TOO WIDE ({spread_pct:.2f}%) — severe slippage risk", warnings)

        # ── CHECK 8: Calculate position size ─────────────────────────────────
        approved_qty, stop_price, target_price, dollar_risk, kelly_size = self._size_position(trade, warnings)

        if approved_qty <= 0:
            return self._block(trade, "POSITION SIZING returned 0 — dollar risk too high", warnings)

        # ── CHECK 9: Max position size ────────────────────────────────────────
        position_value = approved_qty * trade.price
        position_pct   = position_value / self.equity * 100
        if position_pct > self.cfg.MAX_POSITION_SIZE_PCT:
            # Auto-reduce rather than block
            approved_qty = int((self.equity * self.cfg.MAX_POSITION_SIZE_PCT / 100) / trade.price)
            warnings.append(f"Position reduced to {approved_qty} shares to stay under {self.cfg.MAX_POSITION_SIZE_PCT}% limit")

        if approved_qty <= 0:
            return self._block(trade, "After size reduction, qty = 0 — position too large for account", warnings)

        # ── ALL CHECKS PASSED ─────────────────────────────────────────────────
        self.approved_today += 1
        decision = RiskDecision(
            approved=True,
            reason="All risk checks passed",
            ticker=trade.ticker,
            side=trade.side,
            original_qty=trade.qty,
            approved_qty=approved_qty,
            stop_price=round(stop_price, 2),
            target_price=round(target_price, 2),
            dollar_risk=round(dollar_risk, 2),
            kelly_size=round(kelly_size, 2),
            warnings=warnings,
        )
        self._log_decision(trade, decision)

        # Record for duplicate/cooldown tracking
        now = datetime.now()
        self.last_order_time[f"{trade.ticker}_{trade.side}"] = now
        self.ticker_day_count[trade.ticker] = self.ticker_day_count.get(trade.ticker, 0) + 1

        return decision

    # ── Position sizing ───────────────────────────────────────────────────────
    def _size_position(self, trade: TradeRequest, warnings: list):
        """
        Calculates safe position size using:
        1. ATR-based stop placement
        2. Kelly Criterion for account fraction
        3. Volatility adjustment (VIX, IV)
        Returns: (qty, stop_price, target_price, dollar_risk, kelly_qty)
        """
        price = trade.price

        # ── Stop price: ATR-based or default ─────────────────────────────────
        if trade.atr > 0:
            stop_distance = trade.atr * self.cfg.ATR_RISK_MULTIPLIER
            warnings.append(f"ATR stop: ${stop_distance:.2f} below entry (ATR={trade.atr:.2f})")
        else:
            stop_distance = price * (self.cfg.DEFAULT_STOP_PCT / 100)

        if trade.side == "buy":
            stop_price   = price - stop_distance
            target_price = price + (stop_distance * (self.cfg.DEFAULT_TARGET_PCT / self.cfg.DEFAULT_STOP_PCT))
        else:  # sell / short
            stop_price   = price + stop_distance
            target_price = price - (stop_distance * (self.cfg.DEFAULT_TARGET_PCT / self.cfg.DEFAULT_STOP_PCT))

        # ── Kelly Criterion ───────────────────────────────────────────────────
        W  = self.cfg.KELLY_WIN_RATE
        R  = self.cfg.KELLY_AVG_WIN
        kelly_full = W - (1 - W) / R             # Kelly formula: W - (1-W)/R
        kelly_frac = kelly_full * self.cfg.KELLY_FRACTION   # fractional Kelly (safer)
        kelly_dollars = self.equity * max(0, kelly_frac)
        kelly_qty     = int(kelly_dollars / price)

        # ── Volatility adjustment ─────────────────────────────────────────────
        vol_factor = 1.0
        if self.market.vix >= self.cfg.HIGH_VIX:
            vol_factor *= (1 - self.cfg.HIGH_VOL_REDUCTION_PCT / 100)
        if trade.iv > 0.4:  # options IV > 40% → reduce
            vol_factor *= 0.75
            warnings.append(f"High IV ({trade.iv*100:.0f}%) detected — size reduced 25%")

        # ── Dollar risk cap ───────────────────────────────────────────────────
        risk_per_share   = abs(price - stop_price)
        max_qty_by_risk  = int(self.cfg.MAX_TRADE_DOLLAR_RISK / risk_per_share) if risk_per_share > 0 else trade.qty
        kelly_adj_qty    = int(kelly_qty * vol_factor)

        # Final qty = most conservative of: requested, kelly-adjusted, risk-capped
        final_qty    = min(trade.qty, kelly_adj_qty, max_qty_by_risk)
        final_qty    = max(1, final_qty)
        dollar_risk  = final_qty * risk_per_share

        if final_qty < trade.qty:
            warnings.append(f"Qty reduced {trade.qty}→{final_qty} (Kelly: {kelly_adj_qty}, Risk cap: {max_qty_by_risk})")

        return final_qty, stop_price, target_price, dollar_risk, kelly_qty

    # ── Market state updates ──────────────────────────────────────────────────
    def update_market_state(self, vix: float, spy_price: float, spy_prev_price: float = None):
        """Call this every minute with fresh VIX and SPY data"""
        old_vix = self.market.vix
        self.market.vix = vix
        self.market.spy_price = spy_price
        self.market.last_updated = datetime.now().isoformat()

        # Flash crash detection
        if spy_prev_price and spy_prev_price > 0:
            drop_pct = (spy_prev_price - spy_price) / spy_prev_price * 100
            self.market.spy_10min_change = -drop_pct
            if drop_pct >= self.cfg.FLASH_CRASH_DROP_PCT:
                if not self.market.flash_crash_active:
                    log.critical(f"🚨 FLASH CRASH DETECTED — SPY dropped {drop_pct:.1f}% — TRADING HALTED")
                    self.market.flash_crash_active = True
            else:
                if self.market.flash_crash_active:
                    log.info("Flash crash mode cleared — market stabilized")
                self.market.flash_crash_active = False

        # VIX spike alert
        if vix > old_vix * 1.3:
            log.warning(f"⚠ VIX SPIKE: {old_vix:.1f} → {vix:.1f} (+{vix-old_vix:.1f})")

        log.info(f"Market state: VIX={vix:.1f} | SPY=${spy_price:.2f} | Flash crash={'YES' if self.market.flash_crash_active else 'NO'}")

    def update_equity(self, current_equity: float):
        """Call this on every account balance update"""
        self.daily_pnl = current_equity - self.equity_open
        self.equity = current_equity
        drawdown = (self.equity_open - current_equity) / self.equity_open * 100
        if drawdown > self.cfg.MAX_DAILY_DRAWDOWN_PCT * 0.8:
            log.warning(f"⚠ Approaching MDD limit: {drawdown:.1f}% of {self.cfg.MAX_DAILY_DRAWDOWN_PCT}% limit")

    def activate_kill_switch(self, reason: str = "Manual"):
        """Immediately halt all trading"""
        self.kill_active = True
        self.cfg.KILL_SWITCH_ACTIVE = True
        log.critical(f"🛑 KILL SWITCH ACTIVATED — {reason}")

    def reset_kill_switch(self):
        """Re-enable trading (new day / manual reset)"""
        self.kill_active = False
        self.cfg.KILL_SWITCH_ACTIVE = False
        self.blocked_today = 0
        self.approved_today = 0
        self.daily_pnl = 0.0
        self.equity_open = self.equity
        log.info("✅ Kill switch reset — trading enabled")

    def _trigger_close_all(self):
        log.critical(f"🚨 VIX EMERGENCY — triggering CLOSE ALL POSITIONS")
        # In production: call broker API to flatten all positions
        # alpaca_post("/v2/positions", {}) — closes all

    def _block(self, trade: TradeRequest, reason: str, warnings: list) -> RiskDecision:
        self.blocked_today += 1
        log.warning(f"🚫 BLOCKED: {trade.side.upper()} {trade.ticker} — {reason}")
        return RiskDecision(approved=False, reason=reason, ticker=trade.ticker,
                            side=trade.side, original_qty=trade.qty, approved_qty=0, warnings=warnings)

    def _log_decision(self, trade: TradeRequest, d: RiskDecision):
        log.info(f"✅ APPROVED: {d.side.upper()} {d.approved_qty}x {d.ticker} @ ${trade.price:.2f} | Stop: ${d.stop_price:.2f} | Target: ${d.target_price:.2f} | Risk: ${d.dollar_risk:.0f}")
        if d.warnings:
            for w in d.warnings:
                log.warning(f"   ⚠ {w}")

    def record_fill(self, ticker: str, side: str, qty: float, price: float):
        """
        Call this when Alpaca confirms a fill.
        Starts the cooldown timer for that ticker.
        Also tracks short positions for hedge exposure.
        """
        now = datetime.now()
        self.last_fill_time[ticker] = now
        log.info(f"Fill recorded: {side.upper()} {qty}x {ticker} @ ${price:.2f} — cooldown started ({self.cfg.TICKER_COOLDOWN_SECONDS}s)")

        # Track short exposure
        if side == "sell" and ticker not in self.open_positions:
            self.short_positions[ticker] = self.short_positions.get(ticker, 0) + qty * price
        elif side == "buy" and ticker in self.short_positions:
            self.short_positions.pop(ticker, None)

    def status(self) -> dict:
        drawdown = (self.equity_open - self.equity) / self.equity_open * 100
        return {
            "kill_active":     self.kill_active or self.cfg.KILL_SWITCH_ACTIVE,
            "flash_crash":     self.market.flash_crash_active,
            "vix":             self.market.vix,
            "vix_status":      "EMERGENCY" if self.market.vix >= self.cfg.VIX_CLOSE_ALL_THRESHOLD else "RISK-OFF" if self.market.vix >= self.cfg.VIX_RISK_OFF_THRESHOLD else "ELEVATED" if self.market.vix >= self.cfg.HIGH_VIX else "NORMAL",
            "daily_pnl":       round(self.daily_pnl, 2),
            "daily_drawdown":  round(drawdown, 2),
            "mdd_limit":       self.cfg.MAX_DAILY_DRAWDOWN_PCT,
            "equity":          round(self.equity, 2),
            "open_positions":  len(self.open_positions),
            "approved_today":  self.approved_today,
            "blocked_today":   self.blocked_today,
        }


# ─────────────────────────────────────────────────────────────────────────────
# BRACKET ORDER BUILDER  — sends stop + target to broker automatically
# ─────────────────────────────────────────────────────────────────────────────
def build_bracket_order(ticker: str, qty: float, side: str,
                         entry_price: float, stop_price: float, target_price: float) -> dict:
    """
    Builds an Alpaca bracket order JSON.
    The broker executes stop/target SERVER-SIDE — AI does NOT need to watch it.
    This is the #1 most important risk feature.
    """
    return {
        "symbol":        ticker,
        "qty":           str(int(qty)),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
        "order_class":   "bracket",
        "stop_loss": {
            "stop_price": str(round(stop_price, 2))
        },
        "take_profit": {
            "limit_price": str(round(target_price, 2))
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# DEMO / TEST — run this file directly to see risk engine in action
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "="*60)
    print("  KTrade PRO — Risk Engine Demo")
    print("="*60 + "\n")

    equity = 100_000
    engine = RiskEngine(account_equity=equity)

    # Simulate normal market
    engine.update_market_state(vix=18.5, spy_price=548.0, spy_prev_price=547.0)

    trades = [
        TradeRequest(ticker="NVDA", side="buy",  qty=10, price=131.20, conviction=92, atr=3.20, strategy="ORB Breakout"),
        TradeRequest(ticker="TSLA", side="buy",  qty=50, price=228.40, conviction=60, atr=8.10, strategy="Low conviction — should be BLOCKED"),
        TradeRequest(ticker="AAPL", side="buy",  qty=5,  price=195.30, conviction=81, atr=2.40, option_type="CALL", spread=1.80, strategy="Options with wide spread"),
        TradeRequest(ticker="MSFT", side="buy",  qty=8,  price=412.80, conviction=87, atr=5.20, strategy="EMA Confluence"),
    ]

    print("─── FIX 1: Duplicate Order Prevention ───\n")
    # Simulate LITE being sent 4 times at same timestamp (like friend's agent bug)
    lite1 = TradeRequest(ticker="LITE", side="buy", qty=1, price=920.66, conviction=82, strategy="ORB")
    for i in range(4):
        r = engine.evaluate(lite1)
        print(f"  LITE order #{i+1}: {'✅ APPROVED' if r.approved else '🚫 BLOCKED'} — {r.reason}")
    print()

    print("─── FIX 2: Cooldown After Fill ───\n")
    # Record a fill on APLD, then try to re-enter immediately (like friend's 3+111 share bug)
    engine.record_fill("APLD", "buy", 3, 43.73)
    apld_retry = TradeRequest(ticker="APLD", side="buy", qty=111, price=43.71, conviction=80, strategy="Momentum")
    r = engine.evaluate(apld_retry)
    print(f"  APLD re-entry (111 shares immediately after fill): {'✅ APPROVED' if r.approved else '🚫 BLOCKED'}")
    print(f"  Reason: {r.reason}\n")

    print("─── FIX 3: Short/Hedge Check ───\n")
    engine.update_market_state(vix=29.0, spy_price=545.0, spy_prev_price=546.0)
    nvda_buy = TradeRequest(ticker="NVDA", side="buy", qty=10, price=131.20, conviction=88, atr=3.2, strategy="ORB")
    r = engine.evaluate(nvda_buy)
    print(f"  BUY NVDA with VIX=29: {'✅ APPROVED' if r.approved else '🚫 BLOCKED'}")
    if r.warnings:
        for w in r.warnings: print(f"  ⚠  {w}")
    print()

    print("─── Normal Market Conditions (VIX 18.5) ───\n")
    for t in trades:
        r = engine.evaluate(t)
        status = "✅ APPROVED" if r.approved else "🚫 BLOCKED"
        print(f"{status}: {t.side.upper()} {t.ticker}")
        print(f"  Reason: {r.reason}")
        if r.approved:
            print(f"  Qty: {r.original_qty}→{r.approved_qty} | Stop: ${r.stop_price} | Target: ${r.target_price} | Risk: ${r.dollar_risk:.0f}")
            order = build_bracket_order(t.ticker, r.approved_qty, t.side, t.price, r.stop_price, r.target_price)
            print(f"  Bracket order: stop=${order['stop_loss']['stop_price']} | target=${order['take_profit']['limit_price']}")
        if r.warnings:
            for w in r.warnings: print(f"  ⚠  {w}")
        print()

    # Simulate VIX spike
    print("─── VIX Spike to 35 (Risk-Off Mode) ───\n")
    engine.update_market_state(vix=35.0, spy_price=540.0, spy_prev_price=548.0)
    r = engine.evaluate(TradeRequest(ticker="GOOGL", side="buy", qty=5, price=172.50, conviction=85))
    print(f"{'✅ APPROVED' if r.approved else '🚫 BLOCKED'}: BUY GOOGL")
    print(f"  Reason: {r.reason}\n")

    # Simulate flash crash
    print("─── Flash Crash (SPY -3% in 10min) ───\n")
    engine.update_market_state(vix=42.0, spy_price=532.0, spy_prev_price=548.0)
    r = engine.evaluate(TradeRequest(ticker="NVDA", side="buy", qty=5, price=128.0, conviction=90))
    print(f"{'✅ APPROVED' if r.approved else '🚫 BLOCKED'}: BUY NVDA")
    print(f"  Reason: {r.reason}\n")

    print("─── Engine Status ───")
    s = engine.status()
    for k, v in s.items():
        print(f"  {k}: {v}")
    print()
