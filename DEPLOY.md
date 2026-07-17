# Деплой на VPS (Ubuntu/Debian)

Инструкция для обычного VPS (AdminVPS и т.п.) с root-доступом по SSH.
Готовые конфиги лежат в `deploy/`.

Требования к серверу: Python 3.10+, ~2 ГБ свободного диска (SDE ~500 МБ
в распакованном виде + кэш ESI), 1 ГБ RAM достаточно.

## 1. Подключиться и поставить пакеты

```bash
ssh root@<IP-сервера>
apt update && apt install -y python3 python3-venv git nginx apache2-utils
```

## 2. Создать пользователя и склонировать репозиторий

```bash
useradd -m -s /bin/bash eve
git clone https://github.com/malefic89-cell/eve-t1-calc.git /opt/eve-t1-calc
chown -R eve:eve /opt/eve-t1-calc
```

## 3. Виртуальное окружение и зависимости

```bash
sudo -u eve bash -c '
  cd /opt/eve-t1-calc &&
  python3 -m venv .venv &&
  .venv/bin/pip install -r requirements.txt
'
```

## 4. Сервис systemd

```bash
cp /opt/eve-t1-calc/deploy/eve-t1-calc.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now eve-t1-calc
```

Проверка: `systemctl status eve-t1-calc` и `journalctl -u eve-t1-calc -f`.

**Первый запуск долгий**: скачивается SDE (~140 МБ gz), затем ~417 страниц
ордеров Jita и 1738 запросов истории объёмов. Прогресс виден по
`curl -u user:pass http://127.0.0.1:8000/api/status` (локально на сервере —
без auth: `curl http://127.0.0.1:8000/api/status`), ждать `"status":"ready"`.
Всё кэшируется в `/opt/eve-t1-calc/data/` — при перезапуске сервис
поднимается быстро.

## 5. Nginx (реверс-прокси)

```bash
cp /opt/eve-t1-calc/deploy/nginx-eve-t1-calc.conf /etc/nginx/sites-available/eve-t1-calc
# отредактировать server_name: свой домен или IP
nano /etc/nginx/sites-available/eve-t1-calc
ln -s /etc/nginx/sites-available/eve-t1-calc /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

## 6. Пароль на вход (обязательно)

Приложение однопользовательское: настройки скиллов/стендингов общие,
и любой посетитель может их поменять или запустить пересбор данных.
Поэтому доступ закрывается Basic Auth:

```bash
htpasswd -c /etc/nginx/.htpasswd-eve <логин>
systemctl reload nginx
```

## 7. HTTPS (если есть домен)

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d calc.example.com
```

Если домена нет и заходите по IP — можно жить и на http, но пароль из
шага 6 тогда ходит по сети в открытом виде; используйте уникальный.

## Обновление приложения

```bash
cd /opt/eve-t1-calc
sudo -u eve git pull
systemctl restart eve-t1-calc   # нужен только при изменении *.py
```

Если менялся только `static/index.html`, достаточно обновить страницу
в браузере (Ctrl+F5).
