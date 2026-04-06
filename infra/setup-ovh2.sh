#!/usr/bin/env bash
# setup-ovh2.sh — One-time setup for the OVH-2 VPS.
# Run from local machine: ./infra/setup-ovh2.sh
# Requires SSH access configured as "ovh2" in ~/.ssh/config.
set -euo pipefail

REMOTE="ovh2"
REMOTE_DIR="/opt/angi-lister"
SSH_CMD="ssh ${REMOTE}"

echo "==> Running remote setup on ${REMOTE}..."
${SSH_CMD} bash -s <<'SETUP_SCRIPT'
set -euo pipefail

echo "--- Updating system packages ---"
sudo apt-get update -y
sudo apt-get upgrade -y

# --- Docker ---
if ! command -v docker &> /dev/null; then
    echo "--- Installing Docker ---"
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    sudo systemctl enable --now docker
    echo "  Docker installed. You may need to re-login for group membership."
else
    echo "--- Docker already installed ---"
fi

# --- Docker Compose plugin (v2) ---
if ! docker compose version &> /dev/null; then
    echo "--- Installing Docker Compose plugin ---"
    sudo apt-get install -y docker-compose-plugin
else
    echo "--- Docker Compose plugin already installed ---"
fi

# --- Project directory ---
REMOTE_DIR="/opt/angi-lister"
sudo mkdir -p "${REMOTE_DIR}"
sudo chown "$USER":"$USER" "${REMOTE_DIR}"

# --- Firewall ---
echo "--- Configuring UFW firewall ---"
sudo ufw allow 41022/tcp comment 'SSH'
sudo ufw allow 80/tcp comment 'HTTP'
sudo ufw allow 443/tcp comment 'HTTPS'
sudo ufw --force enable
sudo ufw status

echo "--- Setup complete ---"
echo "Next steps:"
echo "  1. Copy project files:  scp -r . ovh2:${REMOTE_DIR}/"
echo "  2. Create .env file:    ssh ovh2 'nano ${REMOTE_DIR}/.env'"
echo "  3. Deploy:              ./infra/deploy.sh"
SETUP_SCRIPT

echo "==> Syncing project files to ${REMOTE}:${REMOTE_DIR}/ ..."
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
    --exclude='venv' --exclude='.env' --exclude='*.pyc' \
    "$(dirname "$0")/.." "${REMOTE}:${REMOTE_DIR}/"

echo ""
echo "==> Remote setup complete."
echo "    IMPORTANT: Create the production .env file on the server:"
echo "      ssh ovh2 'nano ${REMOTE_DIR}/.env'"
echo ""
echo "    Required .env vars:"
echo "      DATABASE_URL=postgresql://angi:<STRONG_PASSWORD>@db:5432/angi_lister"
echo "      POSTGRES_PASSWORD=<STRONG_PASSWORD>"
echo "      ANGI_API_KEY=<PRODUCTION_KEY>"
echo "      RESEND_API_KEY=<YOUR_RESEND_KEY>"
echo "      SENDER_EMAIL=Netic <noreply@mail.discordwell.com>"
echo "      CONSOLE_USER=admin"
echo "      CONSOLE_PASSWORD=<STRONG_PASSWORD>"
