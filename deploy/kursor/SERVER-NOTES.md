# Деплой проектов на сервер KURSOR (Yandex Cloud)

Инструкция для ИИ-агента. Сервер уже настроен и обслуживает боевые проекты —
следуй шагам точно, ничего лишнего не перезапускай.

## Что за сервер

- ВМ в Yandex Cloud Kazakhstan, IP **94.131.94.41** (Ubuntu, Docker + Docker Compose v2).
- Все проекты живут в **/opt/<имя-проекта>**, каждый — свой docker-compose.
- Все контейнеры сидят в одной docker-сети **kursor_default**.
- Порты 80/443 занимает **Caddy** (проект /opt/kursor) — он терминирует HTTPS
  (сертификаты Let's Encrypt автоматом) и проксирует на контейнеры по их именам.
  Свои порты наружу НЕ публиковать.
- Домены — поддомены **kursor.school**, зона в Yandex Cloud DNS
  (id `bos6333ko41f67piitak`, облако cloud-starcraft1312, каталог `ao7qhdu4ui19mqo6hd8f`,
  консоль kz.console.yandex.cloud).

## Доступ по SSH

Ключ лежит на этом Маке: `~/.ssh/kursor_deploy`. Пользователь `deploy`.
**ВАЖНО: исходящий порт 22 в этой сети заблокирован — подключаться ТОЛЬКО на порт 2222.**

```bash
ssh -i ~/.ssh/kursor_deploy -p 2222 deploy@94.131.94.41
```

Ещё блокирован UDP 53: проверять DNS так — `dig +tcp @8.8.8.8 <домен>`, либо dig с самого сервера.

## Деплой НОВОГО проекта (пример: myapp → myapp.kursor.school)

### 1. Залить код

```bash
rsync -az -e "ssh -i ~/.ssh/kursor_deploy -p 2222" \
  --exclude '.git' --exclude '.env' --exclude 'node_modules' \
  --exclude 'data' --exclude '.DS_Store' --exclude '.claude' \
  ./ deploy@94.131.94.41:/opt/myapp/
```

Скрытый мусор (.DS_Store и т.п.) всегда исключать.

### 2. Dockerfile (если в проекте нет) — пример для Node

```dockerfile
FROM node:22-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
EXPOSE 3000
CMD ["npm", "start"]
```

### 3. docker-compose.yml в /opt/myapp

```yaml
services:
  myapp:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/data      # если приложению нужно хранить файлы
networks:
  default:
    name: kursor_default
    external: true
```

Ключевое: сеть external `kursor_default`, никаких `ports:` наружу.
Контейнер получит имя `myapp-myapp-1` — по нему Caddy его найдёт.

### 4. .env создать НА СЕРВЕРЕ

`/opt/myapp/.env` — руками на сервере (heredoc по ssh). Секреты в git/rsync не таскать.

### 5. Собрать и запустить

```bash
ssh -i ~/.ssh/kursor_deploy -p 2222 deploy@94.131.94.41 \
  "cd /opt/myapp && docker compose build && docker compose up -d"
```

ВМ слабая (2 vCPU на 20%, 2 ГБ + swap) — сборка может идти 5–10 минут, ставь таймаут больше.

### 6. DNS-запись

В зоне kursor.school добавить A-запись: имя `myapp`, значение `94.131.94.41`, TTL 300.
Через yc CLI (профиль `kursor-kz` настроен на этом Маке):

```bash
yc --profile kursor-kz dns zone add-records --id bos6333ko41f67piitak \
  --record "myapp.kursor.school. 300 A 94.131.94.41"
```

Если yc недоступен/запрещён — веб-консоль kz.console.yandex.cloud → Cloud DNS → зона kursor.school
(значения TXT с пробелами там вводить в кавычках; SPA консоли подвисает — помогает жёсткая перезагрузка).
Проверка: `dig +tcp @8.8.8.8 myapp.kursor.school` → 94.131.94.41.

### 7. Caddy vhost

Дописать блок в **/opt/kursor/Caddyfile** (НЕ трогая существующие блоки!):

```
myapp.kursor.school {
	encode gzip
	reverse_proxy myapp-myapp-1:3000
}
```

Применить: `docker restart kursor-caddy-1`
(даунтайм других сайтов ~2 секунды; сертификат для нового домена Caddy получит сам,
но только ПОСЛЕ того, как DNS уже резолвится — сначала шаг 6).

### 8. Проверить

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://myapp.kursor.school/
ssh -i ~/.ssh/kursor_deploy -p 2222 deploy@94.131.94.41 "docker logs myapp-myapp-1 --since 5m | tail -20"
```

## ОБНОВЛЕНИЕ существующего проекта

Тот же rsync (шаг 1: те же exclude — на сервере остаются свои .env, data/, Dockerfile,
docker-compose.yml; флаг `--delete` НЕ использовать), затем:

```bash
ssh -i ~/.ssh/kursor_deploy -p 2222 deploy@94.131.94.41 \
  "cd /opt/<проект> && docker compose build && docker compose up -d"
```

## Чего НЕ делать

- Не трогать **/opt/kursor** (движок процессов KURSOR: db+app+caddy, docker compose --profile prod)
  кроме дописывания vhost-блока в Caddyfile.
- Не менять чужие блоки Caddyfile, переменные DOMAIN/BASE_URL, не занимать порты 80/443.
- Не удалять и не перезаписывать серверные `.env` и каталоги `data/` проектов.
- Не коммитить секреты; проверка перед пушем: `git grep -iE "token|secret|password" -- ':!.env*'`.

## Уже задеплоено (для ориентира)

| Проект | Путь | Контейнер | Домен |
|---|---|---|---|
| Движок процессов KURSOR | /opt/kursor | kursor-app-1 (+db, caddy) | processes.kursor.school |
| AdPulse (трекер рекламы) | /opt/adpulse | adpulse-adpulse-1 | adpulse.kursor.school |
