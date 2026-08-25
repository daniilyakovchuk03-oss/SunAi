#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Деплой SunAi на сервер KURSOR (Yandex Cloud) → https://sunai.kursor.school
#
# Запускать С МАКА, из корня проекта:
#     bash deploy/kursor/deploy.sh
#
# Скрипт делает ровно одно: поднимает ЭТОТ проект в /opt/sunai.
# Чужие проекты не трогает: в Caddyfile только дописывает свой блок
# (с бэкапом и проверкой), серверные .env и data/ никогда не перезаписывает.
# ---------------------------------------------------------------------------
set -euo pipefail

SERVER_IP="${SERVER_IP:-94.131.94.41}"
SSH_PORT="${SSH_PORT:-2222}"          # порт 22 в сети KURSOR закрыт — только 2222
SSH_USER="${SSH_USER:-deploy}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/kursor_deploy}"

PROJECT="sunai"
DOMAIN="${DOMAIN:-sunai.kursor.school}"
REMOTE_DIR="/opt/${PROJECT}"
CONTAINER="${PROJECT}-${PROJECT}-1"

CADDY_DIR="/opt/kursor"
CADDYFILE="${CADDY_DIR}/Caddyfile"
CADDY_CONTAINER="kursor-caddy-1"

DNS_ZONE_ID="${DNS_ZONE_ID:-bos6333ko41f67piitak}"
YC_PROFILE="${YC_PROFILE:-kursor-kz}"

SSH_OPTS=(-i "$SSH_KEY" -p "$SSH_PORT" -o ServerAliveInterval=30 -o ServerAliveCountMax=120)
SSH=(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SERVER_IP}")

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0. Проверки перед стартом -------------------------------------------
say "Проверяю доступ"
[[ -f "$SSH_KEY" ]] || die "Нет ключа $SSH_KEY"
[[ -f "Dockerfile" && -d "app" ]] || die "Запускать из корня проекта (нет Dockerfile/app)."
chmod 600 "$SSH_KEY" 2>/dev/null || true
"${SSH[@]}" -o BatchMode=yes -o ConnectTimeout=20 "echo ok" >/dev/null \
  || die "Сервер недоступен по ${SERVER_IP}:${SSH_PORT}. Порт 22 закрыт — нужен 2222."
echo "SSH работает."

# --- 1. Заливаю код -------------------------------------------------------
# --delete НЕ используем: на сервере остаются .env, data/ и прочее своё.
say "Заливаю код в ${REMOTE_DIR}"
"${SSH[@]}" "mkdir -p ${REMOTE_DIR}/data"
rsync -az -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.git' --exclude '.env' --exclude 'node_modules' \
  --exclude 'data' --exclude '.DS_Store' --exclude '.claude' \
  --exclude 'venv' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'backups' --exclude '*.db' \
  ./ "${SSH_USER}@${SERVER_IP}:${REMOTE_DIR}/"

# Серверный compose кладём поверх локального: без ports, в сети kursor_default.
"${SSH[@]}" "cp ${REMOTE_DIR}/deploy/kursor/docker-compose.yml ${REMOTE_DIR}/docker-compose.yml"
echo "Код на месте."

# --- 2. .env — создаём на сервере, существующий не трогаем ----------------
say "Проверяю .env"
"${SSH[@]}" bash -s <<REMOTE_ENV
set -euo pipefail
if [[ -f ${REMOTE_DIR}/.env ]]; then
  echo ".env уже есть — не трогаю."
else
  SECRET="\$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
  cat > ${REMOTE_DIR}/.env <<ENV
# Режим mock: наружу ничего не уходит. Боевой режим включать вручную,
# когда будут ключи Wazzup и подтверждён слот вебхуков.
MODE=mock

WAZZUP_API_KEY=
WAZZUP_CHANNEL_ID=
AI_CRM_USER_ID=ai-agent

AMO_SUBDOMAIN=
AMO_ACCESS_TOKEN=

LLM_PROVIDER=stub
ANTHROPIC_API_KEY=
LLM_MODEL=claude-haiku-4-5-20251001

DB_PATH=/srv/data/agent.db
RULES_PATH=/srv/rules.yaml
REPLY_DELAY_SECONDS=8
WEBHOOK_SECRET=\${SECRET}
TZ=Asia/Almaty
ENV
  chmod 600 ${REMOTE_DIR}/.env
  echo "Создан ${REMOTE_DIR}/.env (MODE=mock, WEBHOOK_SECRET сгенерирован)."
fi
REMOTE_ENV

# --- 3. Сборка и запуск ---------------------------------------------------
# ВМ слабая (2 vCPU / 2 ГБ) — сборка может идти 5-10 минут.
say "Собираю и запускаю контейнер (может занять 5-10 минут)"
"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose build && docker compose up -d"
"${SSH[@]}" "docker ps --filter name=${CONTAINER} --format 'Контейнер: {{.Names}}  {{.Status}}'"

# --- 4. DNS ---------------------------------------------------------------
say "Проверяю DNS для ${DOMAIN}"
dns_ok() {
  # UDP 53 в сети KURSOR закрыт, поэтому dig только через TCP.
  [[ "$(dig +tcp +short @8.8.8.8 "$DOMAIN" A 2>/dev/null | tail -1)" == "$SERVER_IP" ]]
}
if dns_ok; then
  echo "A-запись уже указывает на ${SERVER_IP}."
else
  if command -v yc >/dev/null 2>&1; then
    echo "Добавляю A-запись через yc..."
    yc --profile "$YC_PROFILE" dns zone add-records --id "$DNS_ZONE_ID" \
       --record "${DOMAIN}. 300 A ${SERVER_IP}" \
      || warn "yc не смог добавить запись — возможно, она уже есть."
  else
    warn "yc не найден. Добавьте вручную в kz.console.yandex.cloud → Cloud DNS → kursor.school:"
    warn "    A   ${DOMAIN}   ${SERVER_IP}   TTL 300"
  fi
  echo -n "Жду появления DNS (до 5 минут)"
  for _ in $(seq 1 30); do
    if dns_ok; then echo " — есть."; break; fi
    echo -n "."; sleep 10
  done
fi
dns_ok || die "DNS ещё не резолвится. Caddy не сможет выпустить сертификат — \
добавьте A-запись и запустите скрипт снова (код уже залит, шаги повторяются безопасно)."

# --- 5. Caddy: дописываем свой блок --------------------------------------
say "Настраиваю Caddy"
"${SSH[@]}" bash -s <<REMOTE_CADDY
set -euo pipefail
if grep -qE '^\s*${DOMAIN}\s*\{' ${CADDYFILE}; then
  echo "Блок ${DOMAIN} в Caddyfile уже есть — не дублирую."
  exit 0
fi

BACKUP="${CADDYFILE}.bak-\$(date +%F-%H%M%S)"
cp ${CADDYFILE} "\$BACKUP"
echo "Бэкап Caddyfile: \$BACKUP"

# Дописываем в конец, чужие блоки не трогаем.
cat >> ${CADDYFILE} <<'BLOCK'

${DOMAIN} {
	encode gzip
	reverse_proxy ${CONTAINER}:8000
}
BLOCK

# Если конфиг невалиден — откатываемся, чужие сайты не должны пострадать.
if docker exec ${CADDY_CONTAINER} caddy validate --adapter caddyfile \
     --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  echo "Caddyfile валиден."
elif docker exec ${CADDY_CONTAINER} caddy version >/dev/null 2>&1; then
  cp "\$BACKUP" ${CADDYFILE}
  echo "!! Caddyfile не прошёл проверку — откатил из бэкапа." >&2
  exit 1
else
  echo "Проверить конфиг не удалось (нет caddy validate) — продолжаю."
fi
REMOTE_CADDY

say "Перезапускаю Caddy (даунтайм сайтов ~2 секунды)"
"${SSH[@]}" "docker restart ${CADDY_CONTAINER}" >/dev/null
sleep 12

# --- 6. Проверка ----------------------------------------------------------
say "Проверяю результат"
# Сертификат Let's Encrypt выпускается при первом обращении — даём время.
CODE=000
for _ in $(seq 1 20); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://${DOMAIN}/" || echo 000)"
  [[ "$CODE" =~ ^(200|302|401)$ ]] && break
  sleep 10
done
echo "https://${DOMAIN}/         → ${CODE}"
echo "https://${DOMAIN}/health   → $(curl -s --max-time 20 "https://${DOMAIN}/health" || echo '(нет ответа)')"

# Чужие проекты обязаны продолжать работать.
say "Контрольная проверка соседних проектов"
for d in processes.kursor.school adpulse.kursor.school; do
  echo "  ${d} → $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://${d}/" || echo 000)"
done

say "Логи и пароль администратора"
"${SSH[@]}" "docker logs ${CONTAINER} --since 30m 2>&1 | grep -iE 'администратор|Логин|Пароль|Режим' | tail -5" \
  || echo "(строки не найдены — вероятно, админ создан при более раннем запуске)"

echo ""
if [[ "$CODE" =~ ^(200|302|401)$ ]]; then
  echo "======================================================"
  echo " Готово: https://${DOMAIN}"
  echo " Тестовый чат:  https://${DOMAIN}/"
  echo " Админка:       https://${DOMAIN}/admin"
  echo " Логи:          ssh -i ${SSH_KEY} -p ${SSH_PORT} ${SSH_USER}@${SERVER_IP} 'docker logs -f ${CONTAINER}'"
  echo "======================================================"
else
  warn "Сайт ответил ${CODE}. Смотрите: docker logs ${CONTAINER} --tail 50"
  warn "и docker logs ${CADDY_CONTAINER} --tail 50 (выпуск сертификата)."
  exit 1
fi
