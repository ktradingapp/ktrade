#!/usr/bin/env python3
"""KTrade release-safety + architectural-firewall check.

Two jobs:
  1. Release hygiene: no secret files / leaked keys ship in a package.
  2. Architectural firewall (v13.8): assert KTrade keeps its core safety property —
     it executes ONLY through the approved broker adapter and never gains a
     dynamic-code-execution path or a wallet/contract dependency.

(2) is the honest, non-theatre version of the "external strategy guard" idea: rather
than scanning prose for crypto buzzwords (high false positives, trivially bypassed),
it fails the build if a *mechanism* of bypass is introduced into the code itself —
eval/exec of dynamic input, deserialization RCE, shell execution, a variable-driven
dynamic import, or a web3/wallet/contract library in imports or requirements.

Escape hatch: if a flagged line is genuinely legitimate, append a trailing comment
`# release-safety: allow <reason>` and it is reported as an explicit, documented
exception instead of an error. Ticker strings like ETH-USD/BTC-USD are NOT flagged —
only import statements and dependency names are, so the trading universe is untouched.
"""
from pathlib import Path
import re
import sys

FORBIDDEN_FILES = {".env", "data/ktrade.db", "data/risk_state.db"}

SECRET_PATTERNS = [
    ("OpenAI key", re.compile(r"sk-(?:proj|ant)?-[A-Za-z0-9_\-]{20,}")),
    ("Alpaca key", re.compile(r"(?:ALPACA|APCA).*KEY\s*=\s*(?!PKxxxxxxxx|change-this|your)[A-Za-z0-9_\-]{12,}", re.I)),
    ("Alpaca secret", re.compile(r"(?:ALPACA|APCA).*SECRET\s*=\s*(?!xxxxxxxx|change-this|your)[A-Za-z0-9_\-]{20,}", re.I)),
    ("Finnhub key", re.compile(r"FINNHUB.*KEY\s*=\s*(?!xxxxxxxx|change-this|your)[A-Za-z0-9_\-]{20,}", re.I)),
]

# --- Architectural firewall: dynamic-execution primitives (scanned in .py) -------
# Each targets the DANGEROUS builtin/dynamic form, not safe method look-alikes
# (df.eval, cursor.execute, ast.literal_eval, __import__('uuid') with a literal).
EXEC_PATTERNS = [
    ("dynamic eval()", re.compile(r"(?<![.\w])eval\s*\(")),
    ("dynamic exec()", re.compile(r"(?<![.\w])exec\s*\(")),
    ("pickle deserialization (RCE risk)", re.compile(r"\bpickle\.loads?\s*\(")),
    ("marshal deserialization (RCE risk)", re.compile(r"\bmarshal\.loads?\s*\(")),
    ("shell command execution", re.compile(r"\bos\.system\s*\(")),
    ("subprocess shell=True (injection risk)", re.compile(r"shell\s*=\s*True")),
    ("variable-driven __import__", re.compile(r"__import__\s*\(\s*[^'\")\s]")),
    ("variable-driven importlib", re.compile(r"importlib\.import_module\s*\(\s*[^'\")\s]")),
]

# Wallet / smart-contract / DEX EXECUTION libraries. Presence in imports or
# requirements means an execution path outside the broker adapter — block it.
_CRYPTO = r"(web3|eth[-_]account|ethers|solana|solathon|walletconnect|brownie|eth[-_]keys|mnemonic|bip32utils|bip32|bip39|bitcoinlib|coincurve|ledgereth|web3auth)"
CRYPTO_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+" + _CRYPTO + r"\b", re.I)
CRYPTO_REQ_RE = re.compile(r"^\s*" + _CRYPTO + r"\b", re.I)

ALLOW = "release-safety: allow"

# Files that intentionally contain the very patterns/fixtures being scanned for, so
# they are excluded from the firewall + secret scans (they are first-party and tested).
FIXTURE_FILES = {"test_v137_firewall.py"}
SCANNER_SELF = "check_release_safety.py"

errors = []
allowed_exceptions = []
root = Path.cwd()

for rel in FORBIDDEN_FILES:
    if (root / rel).exists():
        errors.append(f"Forbidden release file present: {rel}")

for path in root.rglob("*"):
    if not path.is_file():
        continue
    if any(part in {".venv", "__pycache__"} for part in path.parts):
        continue
    suffix = path.suffix.lower()
    if suffix not in {".py", ".env", ".txt", ".md", ".template", ".cmd", ".ps1", ".service", ".json"}:
        continue
    relpath = path.relative_to(root)
    text = path.read_text(errors="ignore")

    # 1) secrets (whole-file) — skip files that hold intentional fixtures
    if path.name not in FIXTURE_FILES:
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"Possible {label} in {relpath}")

    # 2) architectural firewall (per-line, so we can honour the allow-comment)
    is_py = suffix == ".py"
    is_req = path.name.lower().startswith("requirements") and suffix == ".txt"
    # The scanner necessarily contains the very patterns it scans for, and fixture
    # files hold intentional bad patterns; both are excluded from the firewall scan
    # (they remain otherwise first-party and reviewed).
    if (is_py or is_req) and path.name != SCANNER_SELF and path.name not in FIXTURE_FILES:
        for i, line in enumerate(text.splitlines(), 1):
            allowed = ALLOW in line
            if is_py:
                for label, pat in EXEC_PATTERNS:
                    if pat.search(line):
                        msg = f"{label} at {relpath}:{i}"
                        (allowed_exceptions if allowed else errors).append(
                            msg + (" [allowed]" if allowed else ""))
                if CRYPTO_IMPORT_RE.search(line):
                    msg = f"wallet/contract library import at {relpath}:{i}"
                    (allowed_exceptions if allowed else errors).append(
                        msg + (" [allowed]" if allowed else ""))
            if is_req and CRYPTO_REQ_RE.search(line) and not line.lstrip().startswith("#"):
                msg = f"wallet/contract dependency in {relpath}:{i} -> {line.strip()}"
                (allowed_exceptions if allowed else errors).append(
                    msg + (" [allowed]" if allowed else ""))

if allowed_exceptions:
    print("Release safety: documented exceptions (allowed):")
    for a in allowed_exceptions:
        print("  ~", a)

if errors:
    print("Release safety check FAILED:")
    for e in errors:
        print("-", e)
    sys.exit(1)
print("Release safety check passed.")
