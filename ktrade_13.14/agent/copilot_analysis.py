#!/usr/bin/env python3
"""KTrade copilot ledger analysis (v13.1).

Turns logs/ktrade_copilot_ledger.jsonl into an actual verdict on whether the
copilot's opinions help, instead of eyeballing JSONL.

Two levels of report:

1. DECISION-LEVEL (works immediately, no price data needed): how often the
   copilot fired, agreed, disagreed, or abstained, and which names it vetoed.
   This alone tells you the layer is alive and how opinionated it is.

2. OUTCOME-SCORED (needs forward prices): because shadow mode TAKES every
   rule-approved BUY regardless of the copilot, each ledger row is a real trade
   with a real outcome. So a copilot SKIP is a free counterfactual: it said
   "don't buy", you bought anyway, and the forward return says whether the veto
   would have helped. We mark each BUY's forward return over a horizon and score:
     - agreement quality  : avg forward return when copilot agreed (BUY);
     - veto precision      : of copilot SKIP/HOLD, how many were followed by a
                             loss (veto correct) vs a gain (veto wrong);
     - net veto benefit    : the pct-pts you'd have gained/lost by SKIPPING the
                             trades the copilot disagreed with.  > 0 => the
                             vetoes would have improved paper P&L.

Forward returns are an ENTRY-decision metric (was buying here good over N days),
which is the right, symmetric basis for comparing rules vs copilot — cleaner than
realized exit P&L, which is contaminated by stop/target timing.

Stdlib-only. Usable as a CLI or imported (e.g. by a backend /copilot/report).

  python agent/copilot_analysis.py                         # decision-level
  python agent/copilot_analysis.py --prices prices.csv     # full verdict
        prices.csv columns: ticker,date,close   (date = YYYY-MM-DD)

To wire a live price source instead of a CSV, pass your own
price_at(ticker, ts_iso, horizon_days) -> float|None into build_report().
"""
import argparse
import json
import os
from datetime import datetime, timedelta

_DISAGREE = ("SKIP", "HOLD")


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load_ledger(path):
    """Read the append-only JSONL ledger into a list of dict rows."""
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def load_prices_csv(path):
    """ticker -> sorted list of (date, close). CSV header: ticker,date,close."""
    prices = {}
    if not path or not os.path.exists(path):
        return prices
    import csv
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                t = str(row["ticker"]).upper().strip()
                d = str(row["date"]).strip()[:10]
                c = float(row["close"])
            except (KeyError, ValueError, TypeError):
                continue
            prices.setdefault(t, []).append((d, c))
    for t in prices:
        prices[t].sort(key=lambda x: x[0])
    return prices


def make_price_at(prices):
    """Build a price_at(ticker, ts_iso, horizon_days) from a prices dict.

    Returns the close on the first trading day on/after (decision_date + horizon).
    """
    def price_at(ticker, ts_iso, horizon_days):
        series = prices.get(str(ticker).upper())
        if not series:
            return None
        try:
            d0 = datetime.fromisoformat(ts_iso).date()
        except Exception:
            try:
                d0 = datetime.strptime(str(ts_iso)[:10], "%Y-%m-%d").date()
            except Exception:
                return None
        target = (d0 + timedelta(days=int(horizon_days))).isoformat()
        for d, c in series:
            if d >= target:
                return c
        return None
    return price_at


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def attribute_outcomes(rows, price_at, horizon_days=5):
    """Add `outcome_return` (percent) to each rule-BUY row we can mark forward.
    Rows that already carry `outcome_return` are left as-is. Returns (rows, n)."""
    n = 0
    for r in rows:
        if r.get("outcome_return") is not None:
            n += 1
            continue
        if str(r.get("rule_action")) != "BUY":
            continue
        entry = r.get("price")
        ts = r.get("ts")
        if not entry or not ts:
            continue
        try:
            entry = float(entry)
            if entry <= 0:
                continue
        except (TypeError, ValueError):
            continue
        fwd = price_at(r.get("ticker"), ts, horizon_days)
        if fwd is None:
            continue
        try:
            r["outcome_return"] = (float(fwd) - entry) / entry * 100.0
            n += 1
        except (TypeError, ValueError):
            continue
    return rows, n


def decision_stats(rows):
    """Counts that need no price data."""
    out = {
        "decisions": 0, "opinions": 0, "agreements": 0, "disagreements": 0,
        "abstains": 0, "vetoes": 0, "by_verdict": {}, "vetoed_tickers": {},
        "agreement_rate": None, "disagreement_rate": None, "abstain_rate": None,
    }
    for r in rows:
        if str(r.get("rule_action")) != "BUY":
            continue
        out["decisions"] += 1
        v = str(r.get("copilot_verdict", "ABSTAIN")).upper()
        out["by_verdict"][v] = out["by_verdict"].get(v, 0) + 1
        if r.get("vetoed"):
            out["vetoes"] += 1
        if v == "ABSTAIN":
            out["abstains"] += 1
            continue
        out["opinions"] += 1
        if v in _DISAGREE:
            out["disagreements"] += 1
            t = str(r.get("ticker", "?")).upper()
            out["vetoed_tickers"][t] = out["vetoed_tickers"].get(t, 0) + 1
        else:
            out["agreements"] += 1
    if out["decisions"]:
        out["abstain_rate"] = round(out["abstains"] / out["decisions"], 3)
    if out["opinions"]:
        out["agreement_rate"] = round(out["agreements"] / out["opinions"], 3)
        out["disagreement_rate"] = round(out["disagreements"] / out["opinions"], 3)
    return out


def _mean(xs):
    return round(sum(xs) / len(xs), 3) if xs else None


def outcome_score(rows):
    """Score copilot opinions against forward outcomes. Only rows with an
    `outcome_return` AND a real opinion (not ABSTAIN) on a rule BUY count.

    Reports BOTH equal-weighted (pct-pts) and notional-weighted (dollars) net veto
    benefit — they can disagree in sign when a wrong veto landed on a large trade
    and the correct vetoes were on small ones. The dollar figure is the one that
    matters for promotion decisions; it needs `notional` in the ledger rows."""
    agree, disagree = [], []
    disagree_dollars = []      # (return_pct, notional) for vetoed trades that have size
    for r in rows:
        if str(r.get("rule_action")) != "BUY":
            continue
        ret = r.get("outcome_return")
        if ret is None:
            continue
        v = str(r.get("copilot_verdict", "ABSTAIN")).upper()
        if v == "ABSTAIN":
            continue
        if v in _DISAGREE:
            disagree.append(float(ret))
            notional = r.get("notional")
            if notional is not None:
                try:
                    disagree_dollars.append((float(ret), float(notional)))
                except (TypeError, ValueError):
                    pass
        else:
            agree.append(float(ret))

    veto_correct = sum(1 for x in disagree if x < 0)   # skipped a loser -> good
    veto_wrong = sum(1 for x in disagree if x > 0)     # skipped a winner -> bad
    net_benefit = round(-sum(disagree), 3) if disagree else 0.0  # equal-weighted, pct-pts
    net_dollars = (round(-sum(ret / 100.0 * notl for ret, notl in disagree_dollars), 2)
                   if disagree_dollars else None)       # notional-weighted, $

    out = {
        "scored": len(agree) + len(disagree),
        "agreements_scored": len(agree),
        "disagreements_scored": len(disagree),
        "agreement_avg_return": _mean(agree),
        "agreement_win_rate": (round(sum(1 for x in agree if x > 0) / len(agree), 3)
                               if agree else None),
        "disagreement_avg_return": _mean(disagree),
        "veto_correct": veto_correct,
        "veto_wrong": veto_wrong,
        "veto_precision": (round(veto_correct / len(disagree), 3) if disagree else None),
        "net_veto_benefit_pct": net_benefit,
        "net_veto_benefit_dollars": net_dollars,
        "notional_coverage": len(disagree_dollars),
        "verdict": _verdict_text(agree, disagree, net_benefit, veto_correct,
                                 veto_wrong, net_dollars),
    }
    return out


def _verdict_text(agree, disagree, net_benefit, veto_correct, veto_wrong, net_dollars=None):
    if not disagree and not agree:
        return "No scored decisions yet — let paper trading and the horizon accrue."
    if not disagree:
        return (f"Copilot agreed with every scored BUY ({len(agree)}); "
                f"avg forward return {_mean(agree)}%. No vetoes to judge yet.")
    direction = ("would have IMPROVED" if net_benefit > 0 else
                 "would have HURT" if net_benefit < 0 else "was neutral for")
    txt = (f"Following the copilot's {len(disagree)} vetoes {direction} paper return by "
           f"{net_benefit:+.2f} pct-pts ({veto_correct} vetoes correct, {veto_wrong} wrong). "
           f"Agreed BUYs ({len(agree)}) averaged {_mean(agree)}%.")
    if net_dollars is not None:
        dollar_dir = "GAINED" if net_dollars > 0 else "LOST" if net_dollars < 0 else "broke even"
        txt += (f" Size-weighted, following the vetoes would have {dollar_dir} "
                f"${abs(net_dollars):,.2f}"
                + ("" if (net_dollars >= 0) == (net_benefit >= 0)
                   else " — OPPOSITE sign to the equal-weighted view, i.e. the wrong "
                        "veto(s) landed on the larger trade(s); trust the dollar figure")
                + ".")
    return txt


def build_report(rows, price_at=None, horizon_days=5):
    n_attributed = 0
    if price_at is not None:
        rows, n_attributed = attribute_outcomes(rows, price_at, horizon_days)
    else:
        n_attributed = sum(1 for r in rows if r.get("outcome_return") is not None)
    report = {
        "horizon_days": horizon_days,
        "outcomes_attributed": n_attributed,
        "decision_stats": decision_stats(rows),
    }
    if n_attributed > 0:
        report["outcome_score"] = outcome_score(rows)
    return report


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #
def format_report(report):
    ds = report["decision_stats"]
    lines = []
    lines.append("=" * 60)
    lines.append("KTrade Copilot Ledger — Analysis")
    lines.append("=" * 60)
    lines.append(f"Decisions logged (rule BUYs) : {ds['decisions']}")
    lines.append(f"  copilot opinions           : {ds['opinions']}  "
                 f"(abstain {ds['abstains']}, rate {ds['abstain_rate']})")
    lines.append(f"  agreed with BUY            : {ds['agreements']}  (rate {ds['agreement_rate']})")
    lines.append(f"  disagreed (SKIP/HOLD)      : {ds['disagreements']}  (rate {ds['disagreement_rate']})")
    lines.append(f"  active-mode vetoes applied : {ds['vetoes']}")
    if ds["by_verdict"]:
        verd = ", ".join(f"{k}={v}" for k, v in sorted(ds["by_verdict"].items()))
        lines.append(f"  verdict mix                : {verd}")
    if ds["vetoed_tickers"]:
        top = sorted(ds["vetoed_tickers"].items(), key=lambda x: -x[1])[:8]
        lines.append("  most-vetoed names          : "
                     + ", ".join(f"{t}({c})" for t, c in top))
    if "outcome_score" in report:
        sc = report["outcome_score"]
        lines.append("-" * 60)
        lines.append(f"Outcome scoring (forward {report['horizon_days']}d, "
                     f"{report['outcomes_attributed']} marked)")
        lines.append(f"  agreed BUYs                : {sc['agreements_scored']}  "
                     f"avg {sc['agreement_avg_return']}%  win {sc['agreement_win_rate']}")
        lines.append(f"  disagreed (vetoed) BUYs    : {sc['disagreements_scored']}  "
                     f"avg {sc['disagreement_avg_return']}%")
        lines.append(f"  vetoes correct / wrong     : {sc['veto_correct']} / {sc['veto_wrong']}  "
                     f"(precision {sc['veto_precision']})")
        lines.append(f"  NET veto benefit (equal-wt) : {sc['net_veto_benefit_pct']:+.2f} pct-pts")
        if sc.get("net_veto_benefit_dollars") is not None:
            lines.append(f"  NET veto benefit ($-weighted): {sc['net_veto_benefit_dollars']:+,.2f}  "
                         f"({sc['notional_coverage']}/{sc['disagreements_scored']} vetoes sized)")
        lines.append("-" * 60)
        lines.append("VERDICT: " + sc["verdict"])
    else:
        lines.append("-" * 60)
        lines.append("No outcomes attributed yet — pass --prices CSV (ticker,date,close) or")
        lines.append("a price_at() source to score whether the copilot's calls were right.")
    lines.append("=" * 60)
    return "\n".join(lines)


def _default_ledger_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.getenv("KTRADE_COPILOT_LEDGER",
                     os.path.join(os.path.dirname(here), "logs", "ktrade_copilot_ledger.jsonl"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Analyze the KTrade copilot ledger.")
    ap.add_argument("--ledger", default=_default_ledger_path())
    ap.add_argument("--prices", default=None, help="CSV ticker,date,close for outcome scoring")
    ap.add_argument("--horizon", type=int, default=5, help="forward horizon in days")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)

    rows = load_ledger(args.ledger)
    price_at = make_price_at(load_prices_csv(args.prices)) if args.prices else None
    report = build_report(rows, price_at=price_at, horizon_days=args.horizon)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        if not rows:
            print(f"(no ledger rows at {args.ledger} — run paper trading with "
                  f"KTRADE_COPILOT_MODE=shadow first)")
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
