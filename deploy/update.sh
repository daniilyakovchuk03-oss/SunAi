#!/usr/bin/env bash
# Обновление после изменений в репозитории:  bash deploy/update.sh
set -euo pipefail
APP_DIR="/opt/wa-agent"
cd "$APP_DIR"

echo "==> Забираю изменения"
sudo -u waagent git pull --ff-only

echo "==> Зависимости"
"$APP_DIR/venv/bin/pip" install --quiet -r requirements.txt

echo "==> Перезапуск"
systemctl restart wa-agent
sleep 3
if systemctl is-active --quiet wa-agent; then
  echo "Готово, служба работает."
  curl -sf localhost:8000/health && echo ""
else
  echo "!! Не запустилось. Смотрите: journalctl -u wa-agent -n 50" >&2
  exit 1
fi
