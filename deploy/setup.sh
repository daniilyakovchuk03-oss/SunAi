#!/usr/bin/env bash
# Первичная установка на чистый Ubuntu/Debian сервер.
# Запускать от root:  sudo bash deploy/setup.sh ваш-домен.kz
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR="/opt/wa-agent"
APP_USER="waagent"

if [[ $EUID -ne 0 ]]; then
  echo "Запустите через sudo: sudo bash deploy/setup.sh домен.kz" >&2
  exit 1
fi

echo "==> Пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx certbot python3-certbot-nginx git curl

echo "==> Пользователь ${APP_USER}"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Каталог ${APP_DIR}"
mkdir -p "$APP_DIR"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SRC" != "$APP_DIR" ]]; then
  cp -r "$SRC"/. "$APP_DIR"/
fi
mkdir -p "$APP_DIR/data"

echo "==> Виртуальное окружение"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "==> Файл .env (пароль администратора генерируется)"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  PWD_GEN="$(head -c 12 /dev/urandom | base64 | tr -d '/+=' | head -c 14)"
  SECRET="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${PWD_GEN}|" "$APP_DIR/.env"
  sed -i "s|^WEBHOOK_SECRET=.*|WEBHOOK_SECRET=${SECRET}|" "$APP_DIR/.env"
  sed -i "s|^DB_PATH=.*|DB_PATH=${APP_DIR}/data/agent.db|" "$APP_DIR/.env"
  sed -i "s|^RULES_PATH=.*|RULES_PATH=${APP_DIR}/rules.yaml|" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  ADMIN_PWD="$PWD_GEN"
else
  echo "==> .env уже есть, не трогаю"
  ADMIN_PWD="(не изменён)"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Служба systemd"
install -m 644 "$APP_DIR/deploy/wa-agent.service" /etc/systemd/system/wa-agent.service
systemctl daemon-reload
systemctl enable --now wa-agent

if [[ -n "$DOMAIN" ]]; then
  echo "==> nginx для ${DOMAIN}"
  sed "s/__DOMAIN__/${DOMAIN}/g" "$APP_DIR/deploy/nginx.conf" > "/etc/nginx/sites-available/wa-agent"
  ln -sf /etc/nginx/sites-available/wa-agent /etc/nginx/sites-enabled/wa-agent
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx

  echo "==> HTTPS-сертификат"
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
          --register-unsafely-without-email --redirect || \
    echo "!! Сертификат не выпущен. Проверьте, что домен указывает на этот сервер."
fi

echo "==> Бэкап базы раз в сутки"
cat > /etc/cron.daily/wa-agent-backup <<EOF
#!/bin/sh
mkdir -p ${APP_DIR}/backups
sqlite3 ${APP_DIR}/data/agent.db ".backup '${APP_DIR}/backups/agent-\$(date +%F).db'" 2>/dev/null || \
  cp ${APP_DIR}/data/agent.db ${APP_DIR}/backups/agent-\$(date +%F).db
find ${APP_DIR}/backups -name 'agent-*.db' -mtime +14 -delete
EOF
chmod +x /etc/cron.daily/wa-agent-backup

sleep 2
echo ""
echo "======================================================"
echo " Установка завершена"
echo ""
echo " Адрес:   ${DOMAIN:+https://$DOMAIN}${DOMAIN:-http://<IP-сервера>:8000}"
echo " Логин:   admin@local"
echo " Пароль:  ${ADMIN_PWD}"
echo ""
echo " Состояние:  systemctl status wa-agent"
echo " Логи:       journalctl -u wa-agent -f"
echo " Обновить:   bash ${APP_DIR}/deploy/update.sh"
echo "======================================================"
systemctl is-active --quiet wa-agent && echo "Служба работает." || \
  echo "!! Служба не запустилась: journalctl -u wa-agent -n 50"
