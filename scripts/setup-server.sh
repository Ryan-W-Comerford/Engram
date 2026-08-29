#!/usr/bin/env bash
# One-time server setup. Run this once on a fresh Ubuntu/Debian server.
# Usage: ssh root@your-server 'bash -s' < scripts/setup-server.sh

set -euo pipefail

echo "==> Installing Docker"
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker

echo "==> Installing Caddy"
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy

echo "==> Creating app directory"
mkdir -p /opt/engram
chown "$SUDO_USER:$SUDO_USER" /opt/engram 2>/dev/null || true

echo "==> Configuring firewall (ufw)"
ufw allow 22/tcp      # SSH
ufw allow 80/tcp      # HTTP (Caddy redirect)
ufw allow 443/tcp     # HTTPS (Caddy) — also carries OTel over HTTP/4318 via otel.<domain>
ufw --force enable

echo ""
echo "Server ready. Next steps:"
echo "  1. Copy your project:  rsync or git clone to /opt/engram"
echo "  2. Create .env:        cp .env.example .env && nano .env"
echo "  3. Deploy:             bash scripts/deploy.sh your-server-ip"
