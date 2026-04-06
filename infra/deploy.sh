#!/usr/bin/env bash
# deploy.sh — Deploy angi-lister to OVH-2
set -euo pipefail

SSH_HOST="15.204.59.61"
SSH_PORT="41022"
SSH_KEY="$HOME/.ssh/ovh2_vps"
SSH_USER="ubuntu"
REMOTE_DIR="/opt/angi-lister"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ssh_cmd() {
    ssh -p "$SSH_PORT" -i "$SSH_KEY" -o ConnectTimeout=10 "${SSH_USER}@${SSH_HOST}" "$@"
}

scp_cmd() {
    scp -P "$SSH_PORT" -i "$SSH_KEY" "$@"
}

# ── Step 1: SSH kicker ──────────────────────────────────────────────────────
echo -e "${YELLOW}Checking SSH connectivity...${NC}"
if ! ssh_cmd "true" 2>/dev/null; then
    echo -e "${RED}SSH unreachable — attempting OVH API reboot...${NC}"
    if [ -f "$HOME/Projects/shared/reboot-vps.sh" ]; then
        bash "$HOME/Projects/shared/reboot-vps.sh" ovh2 --wait
    else
        echo -e "${RED}reboot-vps.sh not found — cannot recover${NC}"
        exit 1
    fi
fi
echo -e "${GREEN}SSH connected.${NC}"

# ── Step 2: Rsync project to server ────────────────────────────────────────
echo -e "${YELLOW}Syncing project files...${NC}"
rsync -avz --delete \
    -e "ssh -p ${SSH_PORT} -i ${SSH_KEY}" \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude '*.db' \
    --exclude '.venv' \
    --exclude 'venv' \
    --exclude '.pytest_cache' \
    --exclude '.ruff_cache' \
    "${PROJECT_DIR}/" "${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}/"
echo -e "${GREEN}Files synced.${NC}"

# ── Step 3: Build and start containers ─────────────────────────────────────
echo -e "${YELLOW}Building and starting containers...${NC}"
ssh_cmd "cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml up -d --build"

# ── Step 4: Wait for DB healthy ────────────────────────────────────────────
echo -e "${YELLOW}Waiting for database...${NC}"
for i in $(seq 1 20); do
    if ssh_cmd "cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml exec -T db pg_isready -U angi -d angi_lister" 2>/dev/null; then
        echo -e "${GREEN}Database healthy.${NC}"
        break
    fi
    if [ "$i" -eq 20 ]; then
        echo -e "${RED}Database failed to become healthy after 20 attempts${NC}"
        exit 1
    fi
    sleep 2
done

# ── Step 5: Run migrations ─────────────────────────────────────────────────
echo -e "${YELLOW}Running Alembic migrations...${NC}"
ssh_cmd "cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml exec -T api alembic upgrade head"
echo -e "${GREEN}Migrations complete.${NC}"

# ── Step 6: Seed demo data ─────────────────────────────────────────────────
echo -e "${YELLOW}Seeding demo data...${NC}"
ssh_cmd "cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml exec -T api python -m scripts.seed"
echo -e "${GREEN}Seed complete.${NC}"

# ── Step 7: Deploy Caddy config ────────────────────────────────────────────
echo -e "${YELLOW}Deploying Caddy config...${NC}"
scp_cmd "${PROJECT_DIR}/infra/caddy.conf" "${SSH_USER}@${SSH_HOST}:/tmp/angi.discordwell.com"
ssh_cmd "sudo mv /tmp/angi.discordwell.com /etc/caddy/sites/angi.discordwell.com && sudo systemctl reload caddy"
echo -e "${GREEN}Caddy reloaded.${NC}"

# ── Step 8: Health check ───────────────────────────────────────────────────
echo -e "${YELLOW}Running health check...${NC}"
sleep 3
for i in $(seq 1 10); do
    if ssh_cmd "curl -sf http://127.0.0.1:8090/healthz" 2>/dev/null; then
        echo ""
        echo -e "${GREEN}=== Deploy successful! ===${NC}"
        echo -e "${GREEN}Live at: https://angi.discordwell.com${NC}"
        echo -e "${GREEN}Console: https://angi.discordwell.com/console${NC}"
        exit 0
    fi
    sleep 2
done

echo -e "${RED}Health check failed!${NC}"
ssh_cmd "cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml logs --tail 30 api"
exit 1
