#!/usr/bin/env bash
set -Eeuo pipefail

# KTrade release deploy script
# Usage:
#   /opt/deploy_ktrade_release.sh <release_version> <zip_path>
#
# Example:
#   /opt/deploy_ktrade_release.sh v13.9-master-3 /root/Ktrade_v13.9-master-3_deploy.zip

VERSION="${1:-}"
ZIP_PATH="${2:-}"

if [ -z "$VERSION" ] || [ -z "$ZIP_PATH" ]; then
  echo "ERROR: usage: $0 <release_version> <zip_path>"
  exit 2
fi

if echo "$VERSION" | grep -q '[[:space:]]'; then
  echo "ERROR: release version must not contain spaces: $VERSION"
  exit 2
fi

if ! echo "$VERSION" | grep -Eq '^v[0-9A-Za-z._-]+$'; then
  echo "ERROR: release version must start with v and only use letters, numbers, dot, underscore, or dash: $VERSION"
  exit 2
fi

if [ ! -f "$ZIP_PATH" ]; then
  echo "ERROR: zip not found: $ZIP_PATH"
  exit 1
fi

BASE_DIR="/opt"
RELEASES_DIR="$BASE_DIR/ktrade_releases"
CURRENT_LINK="$BASE_DIR/ktrade_current"
RELEASE_DIR="$RELEASES_DIR/$VERSION"
TMP_DIR="$RELEASES_DIR/.tmp_$VERSION"

echo "Deploying KTrade $VERSION from $ZIP_PATH"

mkdir -p "$RELEASES_DIR"
rm -rf "$TMP_DIR" "$RELEASE_DIR"
mkdir -p "$TMP_DIR"

echo "Extracting release..."
unzip -q "$ZIP_PATH" -d "$TMP_DIR"

# If the zip contains a single nested top-level folder with app files inside,
# flatten it into the release root.
TOP_LEVEL_COUNT="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')"
if [ "$TOP_LEVEL_COUNT" = "1" ]; then
  ONLY_ITEM="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 | head -1)"
  if [ -d "$ONLY_ITEM" ] && { [ -d "$ONLY_ITEM/backend" ] || [ -d "$ONLY_ITEM/frontend" ] || [ -d "$ONLY_ITEM/agent" ]; }; then
    echo "Flattening single nested project folder: $ONLY_ITEM"
    mv "$ONLY_ITEM" "$RELEASE_DIR"
    rm -rf "$TMP_DIR"
  else
    mv "$TMP_DIR" "$RELEASE_DIR"
  fi
else
  mv "$TMP_DIR" "$RELEASE_DIR"
fi

cd "$RELEASE_DIR"

# Remove accidental nested duplicate version folders like Ktrade_V13.9/.
# This prevents duplicate tests/import-mismatch errors.
echo "Checking for accidental nested duplicate KTrade folders..."
find "$RELEASE_DIR" -maxdepth 1 -type d \( -iname "Ktrade_V*" -o -iname "KTrade_V*" -o -iname "ktrade_v*" -o -iname "ktrade_*" \) | while read -r nested; do
  if [ -d "$nested/backend" ] || [ -d "$nested/frontend" ] || [ -d "$nested/agent" ]; then
    echo "Removing nested duplicate project folder: $nested"
    rm -rf "$nested"
  fi
done

# Basic structure validation before changing the live symlink.
echo "Validating release structure..."
[ -f "$RELEASE_DIR/backend/ktrade_alpaca.py" ] || { echo "ERROR: missing backend/ktrade_alpaca.py"; exit 3; }
[ -f "$RELEASE_DIR/agent/ktrade_agent_v9.py" ] || { echo "ERROR: missing agent/ktrade_agent_v9.py"; exit 3; }
[ -f "$RELEASE_DIR/frontend/KTrade_preview.html" ] || { echo "ERROR: missing frontend/KTrade_preview.html"; exit 3; }

# Copy .env from current/previous release BEFORE services restart.
echo "Ensuring .env exists in new release..."
if [ ! -f "$RELEASE_DIR/.env" ]; then
  if [ -L "$CURRENT_LINK" ] && [ -f "$CURRENT_LINK/.env" ]; then
    echo "Copying .env from current release"
    cp "$CURRENT_LINK/.env" "$RELEASE_DIR/.env"
  else
    OLD_ENV="$(find "$RELEASES_DIR" -maxdepth 2 -name ".env" -type f 2>/dev/null | sort | tail -1 || true)"
    if [ -n "$OLD_ENV" ] && [ -f "$OLD_ENV" ]; then
      echo "Copying .env from $OLD_ENV"
      cp "$OLD_ENV" "$RELEASE_DIR/.env"
    else
      echo "ERROR: No .env found in current or previous releases"
      exit 10
    fi
  fi
fi

# Clean Python cache to avoid pytest/import mismatch.
find "$RELEASE_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$RELEASE_DIR" -name "*.pyc" -delete 2>/dev/null || true

# Prepare venv.
echo "Creating/updating virtual environment..."
python3 -m venv "$RELEASE_DIR/.venv"
source "$RELEASE_DIR/.venv/bin/activate"
python -m pip install --upgrade pip setuptools wheel

if [ -f "$RELEASE_DIR/requirements.txt" ]; then
  echo "Installing requirements.txt..."
  pip install -r "$RELEASE_DIR/requirements.txt"
else
  echo "WARNING: requirements.txt not found; skipping pip install"
fi

# Compile check.
echo "Running Python compile check..."
python -m compileall -q "$RELEASE_DIR/backend" "$RELEASE_DIR/agent" "$RELEASE_DIR/data" "$RELEASE_DIR/risk" 2>/dev/null || {
  echo "ERROR: Python compile check failed"
  exit 4
}

# Optional release safety validation if present.
if [ -f "$RELEASE_DIR/scripts/check_release_safety.py" ]; then
  echo "Running release safety check..."
  python "$RELEASE_DIR/scripts/check_release_safety.py"
fi

# Optional tests. Keep deploy resilient if there are no tests.
if command -v pytest >/dev/null 2>&1; then
  echo "Running tests..."
  if find "$RELEASE_DIR" -maxdepth 3 \( -name "test_*.py" -o -name "*_test.py" \) | grep -q .; then
    pytest -q
  else
    echo "No pytest tests found; skipping."
  fi
fi

deactivate || true

# Apply existing server-side custom fixes only if the file exists.
# Long-term source fixes should live in the selected version folder; this hook is only for legacy safety.
if [ -x "/opt/apply_ktrade_custom_fixes.sh" ]; then
  echo "Applying existing KTrade custom fixes hook..."
  /opt/apply_ktrade_custom_fixes.sh "$RELEASE_DIR"
fi

# Ownership/permissions.
echo "Setting ownership and permissions..."
if id ktrade >/dev/null 2>&1; then
  chown -R ktrade:ktrade "$RELEASE_DIR" || true
  chown ktrade:ktrade "$RELEASE_DIR/.env" || true
fi
chmod 600 "$RELEASE_DIR/.env" || true

# Switch active release.
echo "Switching current release symlink..."
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

# Restart services.
echo "Reloading systemd and restarting KTrade services..."
systemctl daemon-reload

# Stop first to reduce port conflicts.
systemctl stop ktrade-backend.service 2>/dev/null || true
systemctl stop ktrade-agent.service 2>/dev/null || true
systemctl stop ktrade-scheduler.service 2>/dev/null || true
pkill -f "backend/ktrade_alpaca.py" 2>/dev/null || true
sleep 2

systemctl restart ktrade-backend.service
systemctl restart ktrade-agent.service || true
systemctl restart ktrade-scheduler.service || true

# Nginx validation/reload.
if command -v nginx >/dev/null 2>&1; then
  nginx -t
  systemctl reload nginx || systemctl restart nginx || true
fi

# Wait for backend readiness instead of failing too quickly.
echo "Waiting for KTrade backend on 127.0.0.1:5001..."
BACKEND_OK=0

for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:5001/all >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:5001/auto/status >/dev/null 2>&1; then
    echo "Backend is ready."
    BACKEND_OK=1
    break
  fi

  echo "Backend not ready yet... attempt $i/30"
  sleep 2
done

if [ "$BACKEND_OK" != "1" ]; then
  echo "ERROR: backend did not become ready on 127.0.0.1:5001"
  systemctl status ktrade-backend.service --no-pager -l || true
  journalctl -u ktrade-backend.service -n 160 --no-pager || true
  exit 7
fi

echo "Deploy complete: $VERSION"
echo "Current release: $(readlink -f "$CURRENT_LINK")"
