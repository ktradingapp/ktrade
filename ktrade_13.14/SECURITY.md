# KTrade Security Policy — Execution Firewall

## The one rule
**KTrade can *analyze* external code, but it may only *execute* orders through its
approved broker adapter (Alpaca paper/live).** External strategy code, peer-agent
code, or anything from a web page is reference material for human review — never
something KTrade loads and runs.

## Why a keyword scanner is NOT how we enforce this
Scanning submitted text for crypto/wallet buzzwords ("uniswap", "wallet", "withdraw")
is the wrong control: it false-positives on ordinary trading language and research
notes, and it is trivially bypassed by any obfuscated code. It produces false
confidence, not protection.

## How it IS enforced — architecturally
KTrade has, by design, no mechanism to turn external text into executed behaviour:
no `eval`/`exec` of dynamic input, no deserialization of untrusted data, no shell
execution, no variable-driven dynamic import, and no wallet/contract (web3) library
in its dependencies. Execution flows only `signal -> risk engine -> broker adapter`.

`scripts/check_release_safety.py` asserts this firewall stays intact on every run (it
is part of the CI workflow and the validation pipeline). It FAILS the build if anyone
introduces a dynamic-execution primitive or a wallet/contract dependency, while
correctly ignoring safe look-alikes (`ast.literal_eval`, `df.eval`, a literal
`__import__('uuid')`) and crypto *ticker symbols* (BTC-USD, ETH-USD trade through the
broker like any other symbol). A genuinely legitimate flagged line can be documented
with a trailing `# release-safety: allow <reason>` comment.

## What is allowed vs blocked
- Allowed: "read this contract and explain the risk", "summarize this DeFi strategy",
  "compare this with KTrade". (Analysis is fine.)
- Blocked from ever executing: wallet access, smart-contract deployment, liquidity
  deposits, direct token transfers, private keys/seed phrases, or any execution path
  outside the broker adapter.
