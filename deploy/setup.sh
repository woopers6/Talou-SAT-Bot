#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/woopers6/SAT-Bot.git"
APP_DIR="${APP_DIR:-$HOME/SAT-Bot}"

if ! command -v git >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y git
fi

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

if [ "$(swapon --show | wc -l)" -eq 0 ]; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  read -rsp "Paste your Discord bot token: " token
  echo
  sed -i "s|^DISCORD_TOKEN=.*|DISCORD_TOKEN=${token}|" .env
  read -rp "Discord server ID for instant slash commands (press Enter to skip): " guild
  sed -i "s|^GUILD_ID=.*|GUILD_ID=${guild}|" .env
fi

sudo docker compose up -d --build
sudo docker compose logs --tail 30 bot
echo
echo "SAT Bot is running. Follow the logs with: cd $APP_DIR && sudo docker compose logs -f bot"
