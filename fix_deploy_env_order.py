from pathlib import Path

p = Path("server_scripts/deploy_ktrade_release.sh")
s = p.read_text(encoding="utf-8")

env_start = s.find('echo "Ensuring .env exists in new release..."')
if env_start == -1:
    raise SystemExit("ERROR: could not find env block start")

env_end_marker = 'chmod 600 "$RELEASE_DIR/.env" || true'
env_end = s.find(env_end_marker, env_start)
if env_end == -1:
    raise SystemExit("ERROR: could not find env block end")

env_end += len(env_end_marker)
env_block = s[env_start:env_end].strip()

# Remove old env block
s = s[:env_start] + s[env_end:]

env_block = env_block.replace(
    "# Copy .env from current/previous release BEFORE services restart.",
    "# Copy .env from current/previous release AFTER release safety check and before services restart."
)

safety_block = '''
# Optional release safety validation if present.
if [ -f "$RELEASE_DIR/scripts/check_release_safety.py" ]; then
  echo "Running release safety check..."
  python "$RELEASE_DIR/scripts/check_release_safety.py"
fi
'''.strip()

# Remove old safety block if it exists in any simple form
old_safety_pos = s.find('check_release_safety.py')
if old_safety_pos != -1:
    # Find start of the containing if/comment block
    block_start = s.rfind('\n#', 0, old_safety_pos)
    if block_start == -1:
        block_start = s.rfind('\nif ', 0, old_safety_pos)
    if block_start == -1:
        block_start = old_safety_pos

    block_end = s.find('\nfi', old_safety_pos)
    if block_end != -1:
        block_end += len('\nfi')
        s = s[:block_start] + "\n" + s[block_end:]

# Insert safety + env before tests, or before custom fixes if tests marker missing
markers = [
    'echo "Running tests..."',
    '# Run tests',
    '# Apply existing server-side custom fixes',
    'if [ -x "/opt/apply_ktrade_custom_fixes.sh" ]',
]

insert_pos = -1
for m in markers:
    insert_pos = s.find(m)
    if insert_pos != -1:
        break

if insert_pos == -1:
    raise SystemExit("ERROR: could not find insertion point before tests/custom fixes")

insert_text = "\n\n" + safety_block + "\n\n" + env_block + "\n\n"

s = s[:insert_pos] + insert_text + s[insert_pos:]

p.write_text(s, encoding="utf-8")
print("OK: fixed deploy script order")