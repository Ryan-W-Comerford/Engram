#!/usr/bin/env bash
# Deploy or update Engram on a remote server.
# Usage: bash scripts/deploy.sh <server-ip-or-hostname> [ssh-user]
#
# SECRETS: .env is intentionally excluded from rsync.
#   On first deploy, create /opt/engram/.env on the server manually:
#     ssh user@server "nano /opt/engram/.env"
#   Then chmod 600 /opt/engram/.env so only the deploy user can read it.
#   Never commit .env to git.

set -euo pipefail

SERVER="${1:?Usage: deploy.sh <server> [user]}"
USER="${2:-deploy}"
REMOTE="$USER@$SERVER"
APP_DIR="/opt/engram"

echo "==> Syncing files to $REMOTE:$APP_DIR"
rsync -az --delete \
  --exclude='.env' \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.venv' \
  --exclude='.pytest_cache' \
  --exclude='.DS_Store' \
  --exclude='.claude' \
  . "$REMOTE:$APP_DIR"

echo "==> Deploying on server"
ssh "$REMOTE" bash -s << EOF
  set -euo pipefail
  cd $APP_DIR

  # First deploy: check .env exists
  if [ ! -f .env ]; then
    echo ""
    echo "ERROR: .env not found at $APP_DIR/.env"
    echo "Create it from .env.example and fill in all required values:"
    echo "  cp .env.example .env && nano .env && chmod 600 .env"
    exit 1
  fi

  echo "--> Pulling latest images"
  docker compose -f docker-compose.yml -f docker-compose.prod.yml pull --ignore-pull-failures 2>/dev/null || true

  echo "--> Building services"
  docker compose -f docker-compose.yml -f docker-compose.prod.yml build

  echo "--> Reloading Caddy config"
  caddy validate --config Caddyfile
  cp Caddyfile /etc/caddy/Caddyfile
  systemctl reload caddy

  COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

  echo "--> Starting stack"
  # --wait blocks until every service with a healthcheck is healthy (or fails).
  \$COMPOSE up -d --wait

  echo "--> Running migrations"
  # -T disables pseudo-TTY allocation — required when run over a non-interactive
  # ssh heredoc, otherwise docker exec errors with "the input device is not a TTY".
  \$COMPOSE exec -T ingestor alembic upgrade head

  echo ""
  echo "Deploy complete."
  \$COMPOSE ps
EOF

echo ""
echo "Done. Dashboard: https://dashboard.yourdomain.com"
