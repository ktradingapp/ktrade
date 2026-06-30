#!/usr/bin/env python3
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

errors = []
root = Path.cwd()
for rel in FORBIDDEN_FILES:
    if (root / rel).exists():
        errors.append(f"Forbidden release file present: {rel}")

for path in root.rglob("*"):
    if not path.is_file():
        continue
    if any(part in {".venv", "__pycache__"} for part in path.parts):
        continue
    if path.suffix.lower() not in {".py", ".env", ".txt", ".md", ".template", ".cmd", ".ps1", ".service", ".json"}:
        continue
    text = path.read_text(errors="ignore")
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            errors.append(f"Possible {label} in {path.relative_to(root)}")

if errors:
    print("Release safety check failed:")
    for e in errors:
        print("-", e)
    sys.exit(1)
print("Release safety check passed.")
