#!/usr/bin/env python3
"""KTrade strategy/validation report assembly (v13.8).

Pure, dependency-light core for the validation pipeline. It takes the results of
each pipeline stage and produces (a) an overall verdict and (b) a markdown report.
Kept separate from the runner so this logic is fully unit-testable offline, with no
subprocess, data, or vectorbt dependency.

A "stage result" is a dict: {"name": str, "status": PASS|FAIL|WARN|SKIP, "detail": str}.

Verdict logic (deliberately conservative):
  - any FAIL                -> NOT_READY   (a check broke; fix before proceeding)
  - any WARN (no FAIL)      -> REVIEW      (ran clean but flagged something, e.g. a
                                            FRAGILE strategy — a human decision)
  - all PASS/SKIP           -> READY       (every executed check passed)
SKIP never blocks: stages skip when their prerequisites (price data on the VPS, a
copilot ledger from paper trading) aren't present in this environment.
"""
from datetime import datetime, timezone

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

_ICON = {PASS: "PASS ", FAIL: "FAIL ", WARN: "WARN ", SKIP: "skip "}


def overall_verdict(stages):
    statuses = [s.get("status") for s in stages]
    if FAIL in statuses:
        return "NOT_READY"
    if WARN in statuses:
        return "REVIEW"
    return "READY"


def verdict_blurb(verdict):
    return {
        "READY": "All executed checks passed. Safe to proceed to the next "
                 "paper-trading / training step.",
        "REVIEW": "Checks ran clean but flagged something (see WARN rows). This is a "
                  "human decision, not an automatic stop — review before proceeding.",
        "NOT_READY": "One or more checks FAILED. Do not proceed until they are fixed.",
    }[verdict]


def assemble_report(stages, meta=None):
    """Return (markdown_report, verdict)."""
    meta = meta or {}
    verdict = overall_verdict(stages)
    ts = meta.get("generated_at") or datetime.now(timezone.utc).isoformat()

    n_pass = sum(1 for s in stages if s.get("status") == PASS)
    n_fail = sum(1 for s in stages if s.get("status") == FAIL)
    n_warn = sum(1 for s in stages if s.get("status") == WARN)
    n_skip = sum(1 for s in stages if s.get("status") == SKIP)

    lines = []
    lines.append("# KTrade Strategy Validation Report")
    lines.append("")
    lines.append(f"- Generated: {ts}")
    if meta.get("version"):
        lines.append(f"- KTrade version: {meta['version']}")
    if meta.get("context"):
        lines.append(f"- Context: {meta['context']}")
    lines.append(f"- Verdict: **{verdict}** — {verdict_blurb(verdict)}")
    lines.append(f"- Tally: {n_pass} passed, {n_fail} failed, {n_warn} warnings, {n_skip} skipped")
    lines.append("")
    lines.append("## Stages")
    lines.append("")
    for s in stages:
        st = s.get("status", SKIP)
        lines.append(f"- **[{_ICON.get(st, st)}] {s.get('name', '?')}** — {s.get('detail', '')}")
    lines.append("")

    if n_skip:
        lines.append("## Skipped stages")
        lines.append("")
        lines.append("Stages skip when their inputs aren't present here. The data-dependent "
                     "stages (backtest, fragility) need real price history and vectorbt, so they "
                     "run on the VPS when you pass `--prices`. The copilot-analysis stage needs a "
                     "shadow-mode ledger accumulated from paper trading. None of these can run "
                     "offline without those inputs — that's expected, not a failure.")
        lines.append("")

    lines.append("## What this report does and does not tell you")
    lines.append("")
    lines.append("This confirms the agent is **correct, safe, and not obviously curve-fit**. It "
                 "does **not** establish that the strategy is profitable — that comes only from "
                 "out-of-sample results and real paper-trading outcomes accumulated over time. A "
                 "READY verdict means 'clear to keep training/paper-trading', not 'has an edge'.")
    lines.append("")
    return "\n".join(lines), verdict
