# Деплой SunAi на сервер KURSOR → sunai.kursor.school

Одна команда, запускать **с Мака из корня проекта**:

```bash
bash deploy/kursor/deploy.sh
```

Нужен ключ `~/.ssh/kursor_deploy` (другой путь — `SSH_KEY=... bash deploy/kursor/deploy.sh`).

## Что делает скрипт

1. Заливает код в `/opt/sunai` (rsync, без `--delete`).
2. Кладёт серверный `docker-compose.yml` — без публикации портов, в сети `kursor_default`.
3. Создаёт `/opt/sunai/.env`, **если его ещё нет**. Существующий не трогает.
4. Собирает образ и поднимает контейнер `sunai-sunai-1`.
5. Добавляет A-запись `sunai.kursor.school → 94.131.94.41` и ждёт, пока DNS отзовётся.
6. Дописывает свой блок в `/opt/kursor/Caddyfile` и перезапускает Caddy.
7. Проверяет, что сайт открылся — и что соседние проекты живы.

Скрипт можно запускать повторно: он идемпотентен. Это же и обновление после правок.

## Чужие проекты

Скрипт трогает только `/opt/sunai` и один блок в `Caddyfile`:

- перед правкой Caddyfile делается бэкап `Caddyfile.bak-<дата>`;
- если блок `sunai.kursor.school` уже есть — он не дублируется;
- конфиг проверяется `caddy validate`, и при ошибке откатывается из бэкапа;
- серверные `.env` и `data/` не перезаписываются никогда;
- порты 80/443 не занимаются, `ports:` в compose нет.

Перезапуск Caddy — общий для всех сайтов, даунтайм около двух секунд.

## Порядок важен

Caddy выпускает сертификат Let's Encrypt только после того, как домен уже
резолвится. Поэтому DNS (шаг 5) идёт до перезапуска Caddy (шаг 6). Если
A-записи нет и `yc` недоступен, скрипт остановится и попросит добавить её
вручную — после этого запустите его снова.

## Режим

Первый деплой поднимается с `MODE=mock`: наружу не уходит ничего, работает
тестовый чат и админка. Боевой режим включается вручную на сервере, когда
появятся ключи Wazzup и подтвердится слот вебхуков:

```bash
ssh -i ~/.ssh/kursor_deploy -p 2222 deploy@94.131.94.41
sudo nano /opt/sunai/.env          # MODE=live, WAZZUP_API_KEY, WAZZUP_CHANNEL_ID
cd /opt/sunai && docker compose up -d
```

Вебхук для Wazzup после этого: `https://sunai.kursor.school/webhook/wazzup`

## Полезное

```bash
SSH="ssh -i ~/.ssh/kursor_deploy -p 2222 deploy@94.131.94.41"

$SSH "docker logs -f sunai-sunai-1"                  # логи
$SSH "cd /opt/sunai && docker compose restart"       # перезапуск
$SSH "docker ps --filter name=sunai"                 # состояние
curl https://sunai.kursor.school/health              # проверка снаружи
```

Пароль администратора генерируется при первом запуске и печатается в логи —
`docker logs sunai-sunai-1 | grep Пароль`. Сохраните его.

Детали про сервер — в [SERVER-NOTES.md](SERVER-NOTES.md).
