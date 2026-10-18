#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/avito-iphone-bot
APP_USER=avito
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: sudo bash deploy/setup.sh"
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip rsync tzdata

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

mkdir -p "$APP_DIR"
rsync -a --delete --exclude /.venv --exclude /data --exclude /.git --exclude /.env --exclude /tools "$SRC_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/data"
if [ -d "$SRC_DIR/data" ] && [ "$SRC_DIR" != "$APP_DIR" ]; then
  rsync -a --ignore-existing "$SRC_DIR/data"/ "$APP_DIR/data"/
fi
if [ ! -f "$APP_DIR/.env" ]; then
  if [ -f "$SRC_DIR/.env" ]; then cp "$SRC_DIR/.env" "$APP_DIR/.env"; else cp "$APP_DIR/.env.example" "$APP_DIR/.env"; fi
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" -m playwright install-deps chromium
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m playwright install chromium

install -m 644 "$APP_DIR/deploy/avito-bot.service" /etc/systemd/system/avito-bot.service
systemctl daemon-reload
systemctl enable avito-bot

if ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 8080/tcp
fi

echo
echo "Готово. Дальше:"
echo "  1) nano $APP_DIR/.env  — проверьте HEADLESS=true, TG_PROXY= (пусто), PUBLIC_URL=http://IP_СЕРВЕРА:8080,"
echo "     PROXY_URL=http://логин:пароль@адрес:порт, PROXY_ROTATE_URL=ссылка смены IP, FREE_PROXIES=false"
echo "  2) systemctl restart avito-bot, затем вход в Авито: /browser в Telegram → войти по ссылке → /resume"
echo "  3) journalctl -u avito-bot -f   — лог в реальном времени (также $APP_DIR/data/bot.log)"
