#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_HOST="${DEPLOY_SSH_HOST:-ovh2}"
REMOTE_PATH="/opt/angi-lister"
REBOOT_SCRIPT="${HOME}/Projects/shared/reboot-vps.sh"

# SSH kicker: test connectivity, reboot via OVH API if unreachable
ensure_ssh() {
  if ssh -o ConnectTimeout=10 -o BatchMode=yes "$SSH_HOST" "true" 2>/dev/null; then
    return 0
  fi
  echo "SSH unreachable — kicking server via OVH API..."
  if [[ -x "$REBOOT_SCRIPT" ]]; then
    "$REBOOT_SCRIPT" ovh2 --wait
  else
    echo "ERROR: reboot script not found: $REBOOT_SCRIPT" >&2
    exit 1
  fi
}

echo "=== Angi-Lister Deploy ==="
ensure_ssh
echo ""

# Step 1: Sync project to server
echo ">> Syncing to ${SSH_HOST}:${REMOTE_PATH}..."
ssh "${SSH_HOST}" "mkdir -p ${REMOTE_PATH}"
rsync -az --delete \
  --exclude='.git/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='*.db' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='pgdata/' \
  -e "ssh" \
  "$SCRIPT_DIR/" \
  "${SSH_HOST}:${REMOTE_PATH}/"

# Step 2: Build and start containers
echo ""
echo ">> Building and starting containers..."
ssh "${SSH_HOST}" "cd ${REMOTE_PATH} && docker compose -f docker-compose.prod.yml up -d --build"

# Step 3: Run migrations + seed
echo ""
echo ">> Running migrations..."
sleep 5
ssh "${SSH_HOST}" "cd ${REMOTE_PATH} && docker compose -f docker-compose.prod.yml exec -T api alembic upgrade head"
echo ">> Seeding demo data..."
ssh "${SSH_HOST}" "cd ${REMOTE_PATH} && docker compose -f docker-compose.prod.yml exec -T api python -m scripts.seed"

# Step 4: Sync Caddy config and reload
echo ""
echo ">> Updating Caddy config..."
scp -q "$SCRIPT_DIR/infra/caddy.conf" "${SSH_HOST}:/tmp/angi.discordwell.com"
ssh "${SSH_HOST}" "sudo mv /tmp/angi.discordwell.com /etc/caddy/sites/angi.discordwell.com && sudo systemctl reload caddy"

# Step 5: Health check
echo ""
echo ">> Checking health..."
sleep 3
ssh "${SSH_HOST}" "curl -sf http://127.0.0.1:8090/healthz > /dev/null && echo 'API: OK' || echo 'API: FAIL'"

echo ""
echo "=== Deploy complete ==="
echo "App:     https://angi.discordwell.com"
echo "Console: https://angi.discordwell.com/console"
