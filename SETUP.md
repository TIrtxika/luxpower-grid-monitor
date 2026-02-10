# LuxPower Monitoring - Покрокова інструкція встановлення

## Передумови

### Що потрібно мати:
- [ ] VPS з Debian (публічна IP адреса)
- [ ] Raspberry Pi Zero W 2 (підключена до локальної мережі YOUR_LOCAL_NETWORK/24)
- [ ] Доступ до інвертора LuxPower (YOUR_INVERTER_IP:8000)
- [ ] Telegram акаунт для створення ботів

### Інформація для заповнення:

```
VPS_PUBLIC_IP=_______________     # Публічна IP вашого VPS
VPS_SSH_PORT=22                   # SSH порт VPS
RPI_LOCAL_IP=YOUR_RPI_LOCAL_IP        # Локальна IP адреса RPi
INVERTER_IP=YOUR_INVERTER_IP         # IP інвертора (вже відомо)
```

---

## Етап 1: WireGuard VPN

### 1.1 Налаштування WireGuard Server на VPS (Debian)

```bash
# Підключіться до VPS
ssh root@YOUR_VPS_IP

# Встановіть WireGuard
apt update
apt install -y wireguard

# Генерація ключів сервера
cd /etc/wireguard
wg genkey | tee server_private.key | wg pubkey > server_public.key
chmod 600 server_private.key

# Перегляньте ключі (знадобляться далі)
echo "Server Private Key: $(cat server_private.key)"
echo "Server Public Key: $(cat server_public.key)"
```

Створіть конфіг сервера:

```bash
cat > /etc/wireguard/wg0.conf << 'EOF'
[Interface]
Address = 10.10.0.1/24
ListenPort = 51820
PrivateKey = <SERVER_PRIVATE_KEY>

# Дозволити форвардинг до локальної мережі RPi
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

[Peer]
# Raspberry Pi
PublicKey = <RPI_PUBLIC_KEY>
AllowedIPs = 10.10.0.2/32, YOUR_LOCAL_NETWORK/24
EOF
```

**Замініть**:
- `<SERVER_PRIVATE_KEY>` - приватний ключ сервера
- `<RPI_PUBLIC_KEY>` - публічний ключ RPi (створимо далі)
- `eth0` - на ваш мережевий інтерфейс (`ip a` щоб перевірити)

```bash
# Увімкніть IP forwarding
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
sysctl -p

# Відкрийте порт у firewall (якщо є)
ufw allow 51820/udp

# Запустіть WireGuard
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

# Перевірте статус
wg show
```

### 1.2 Налаштування WireGuard Client на Raspberry Pi

```bash
# Підключіться до RPi
ssh pi@YOUR_RPI_LOCAL_IP

# Встановіть WireGuard
sudo apt update
sudo apt install -y wireguard

# Генерація ключів клієнта
cd /etc/wireguard
sudo su
wg genkey | tee client_private.key | wg pubkey > client_public.key
chmod 600 client_private.key

# Перегляньте ключі
echo "Client Private Key: $(cat client_private.key)"
echo "Client Public Key: $(cat client_public.key)"
```

**⚠️ ВАЖЛИВО**: Скопіюйте `Client Public Key` і додайте його в конфіг сервера на VPS!

Створіть конфіг клієнта на RPi:

```bash
cat > /etc/wireguard/wg0.conf << 'EOF'
[Interface]
Address = 10.10.0.2/24
PrivateKey = <CLIENT_PRIVATE_KEY>

[Peer]
# VPS Server
PublicKey = <SERVER_PUBLIC_KEY>
Endpoint = <VPS_PUBLIC_IP>:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
EOF
```

**Замініть**:
- `<CLIENT_PRIVATE_KEY>` - приватний ключ RPi
- `<SERVER_PUBLIC_KEY>` - публічний ключ VPS сервера
- `<VPS_PUBLIC_IP>` - публічна IP адреса вашого VPS

```bash
# Запустіть WireGuard на RPi
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0

# Перевірте статус
sudo wg show
```

### 1.3 Перевірка з'єднання

На RPi:
```bash
# Пінг VPS через тунель
ping 10.10.0.1
```

На VPS:
```bash
# Пінг RPi через тунель
ping 10.10.0.2

# Пінг інвертора через RPi (якщо налаштовано маршрутизацію)
ping YOUR_INVERTER_IP
```

**Очікуваний результат**: Пінги проходять в обидва боки.

---

## Етап 2: Raspberry Pi (Data Collector + API)

### 2.1 Встановлення залежностей

```bash
ssh pi@YOUR_RPI_LOCAL_IP

# Оновлення системи
sudo apt update && sudo apt upgrade -y

# Встановлення Python та залежностей
sudo apt install -y python3 python3-pip python3-venv

# Створення директорії проекту
mkdir -p ~/luxpower
cd ~/luxpower

# Створення віртуального середовища
python3 -m venv venv
source venv/bin/activate

# Встановлення пакетів
pip install flask gunicorn requests
```

### 2.2 Копіювання файлів

Скопіюйте файли з `telegram-bot/rpi/` на Raspberry Pi:

```bash
# На вашій локальній машині (де ви читаєте цю інструкцію)
scp -r telegram-bot/rpi/* pi@YOUR_RPI_LOCAL_IP:~/luxpower/
```

### 2.3 Конфігурація

```bash
# На RPi
cd ~/luxpower
cp config.example.py config.py
nano config.py
```

Відредагуйте `config.py`:
```python
# Інвертор
INVERTER_IP = "YOUR_INVERTER_IP"
INVERTER_PORT = 8000
DONGLE_SERIAL = "YOUR_DONGLE_SERIAL"
INVERTER_SERIAL = "YOUR_INVERTER_SERIAL"

# API
API_HOST = "0.0.0.0"
API_PORT = 8080
API_TOKEN = "your-secret-token-here"  # Згенеруйте: python3 -c "import secrets; print(secrets.token_hex(32))"

# Collector
POLL_INTERVAL = 30  # секунд
```

### 2.4 Тестування

```bash
cd ~/luxpower
source venv/bin/activate

# Тест підключення до інвертора
python3 -c "from inverter import LuxPowerInverter; inv = LuxPowerInverter(); print(inv.get_status())"

# Запуск API для тесту
python3 api.py
```

В іншому терміналі:
```bash
curl http://localhost:8080/status
```

### 2.5 Налаштування systemd сервісів

```bash
# Копіювання сервісів
sudo cp systemd/*.service /etc/systemd/system/

# Редагування шляхів (якщо потрібно)
sudo nano /etc/systemd/system/luxpower-collector.service
sudo nano /etc/systemd/system/luxpower-api.service

# Перезавантаження systemd
sudo systemctl daemon-reload

# Запуск сервісів
sudo systemctl enable luxpower-collector
sudo systemctl enable luxpower-api
sudo systemctl start luxpower-collector
sudo systemctl start luxpower-api

# Перевірка статусу
sudo systemctl status luxpower-collector
sudo systemctl status luxpower-api
```

### 2.6 Перевірка з VPS

На VPS:
```bash
# Запит до API через WireGuard
curl -H "Authorization: Bearer your-secret-token-here" http://10.10.0.2:8080/status
```

---

## Етап 3: VPS (Telegram Bot + Database)

### 3.1 Створення Telegram ботів

1. Відкрийте Telegram і знайдіть `@BotFather`
2. Створіть **Public бот**:
   ```
   /newbot
   Назва: Grid Status Bot
   Username: YourGridStatusBot
   ```
   Збережіть токен: `PUBLIC_BOT_TOKEN`

3. Створіть **Private бот**:
   ```
   /newbot
   Назва: My Inverter Bot
   Username: YourInverterBot
   ```
   Збережіть токен: `PRIVATE_BOT_TOKEN`

4. Дізнайтеся свій Telegram ID:
   - Напишіть боту `@userinfobot`
   - Збережіть свій ID: `ADMIN_CHAT_ID`

### 3.2 Встановлення PostgreSQL

```bash
ssh root@YOUR_VPS_IP

# Встановлення PostgreSQL
apt install -y postgresql postgresql-contrib

# Створення бази даних
sudo -u postgres psql << EOF
CREATE USER luxpower WITH PASSWORD 'your-db-password';
CREATE DATABASE luxpower OWNER luxpower;
\c luxpower
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO luxpower;
EOF
```

### 3.3 Встановлення залежностей бота

```bash
# Створення користувача (рекомендовано)
useradd -m -s /bin/bash luxpower
su - luxpower

# Створення директорії
mkdir -p ~/bot
cd ~/bot

# Віртуальне середовище
python3 -m venv venv
source venv/bin/activate

# Встановлення пакетів
pip install python-telegram-bot psycopg2-binary aiohttp matplotlib
```

### 3.4 Копіювання файлів

```bash
# На вашій локальній машині
scp -r telegram-bot/vps/* root@YOUR_VPS_IP:/home/luxpower/bot/
```

### 3.5 Конфігурація

```bash
su - luxpower
cd ~/bot
cp config.example.py config.py
nano config.py
```

Відредагуйте `config.py`:
```python
# Telegram боти
PUBLIC_BOT_TOKEN = "your-public-bot-token"
PRIVATE_BOT_TOKEN = "your-private-bot-token"
ADMIN_CHAT_IDS = [YOUR_TELEGRAM_ID]  # Ваш Telegram ID

# База даних
DATABASE_URL = "postgresql://luxpower:your-db-password@localhost/luxpower"

# RPi API
RPI_API_URL = "http://10.10.0.2:8080"
RPI_API_TOKEN = "your-secret-token-here"  # Той самий що на RPi

# Алерти
ALERT_SOC_LOW = 20  # Сповіщення коли SOC < 20%
ALERT_TEMP_HIGH = 60  # Сповіщення коли температура > 60°C
```

### 3.6 Ініціалізація бази даних

```bash
cd ~/bot
source venv/bin/activate
python3 database.py --init
```

### 3.7 Тестування бота

```bash
# Запуск для тесту
python3 bot.py
```

Відкрийте Telegram і напишіть вашому боту `/status`.

### 3.8 Налаштування systemd сервісу

```bash
# Від root
sudo cp /home/luxpower/bot/systemd/luxpower-bot.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable luxpower-bot
sudo systemctl start luxpower-bot

# Перевірка
sudo systemctl status luxpower-bot
journalctl -u luxpower-bot -f
```

---

## Етап 4: Фінальна перевірка

### 4.1 Checklist

- [ ] WireGuard тунель працює (ping 10.10.0.1 ↔ 10.10.0.2)
- [ ] RPi читає дані з інвертора
- [ ] RPi API відповідає на запити з VPS
- [ ] PostgreSQL працює
- [ ] Public бот відповідає на `/status`
- [ ] Private бот відповідає на `/status` з детальною інформацією
- [ ] Алерти працюють (тест: вимкніть мережу на хвилину)

### 4.2 Корисні команди для діагностики

**WireGuard**:
```bash
# Статус тунелю
sudo wg show

# Перезапуск
sudo systemctl restart wg-quick@wg0
```

**RPi сервіси**:
```bash
# Логи collector
journalctl -u luxpower-collector -f

# Логи API
journalctl -u luxpower-api -f

# Перезапуск
sudo systemctl restart luxpower-collector luxpower-api
```

**VPS бот**:
```bash
# Логи бота
journalctl -u luxpower-bot -f

# Перезапуск
sudo systemctl restart luxpower-bot
```

### 4.3 Моніторинг

Рекомендую налаштувати простий моніторинг:

```bash
# На VPS - перевірка чи RPi доступна
*/5 * * * * ping -c 1 10.10.0.2 > /dev/null || echo "RPi недоступна" | mail -s "Alert" your@email.com
```

---

## Вирішення проблем

### WireGuard не з'єднується

1. Перевірте firewall на VPS: `ufw status`
2. Перевірте що порт 51820/UDP відкритий
3. Перевірте ключі (публічні/приватні не переплутані)
4. Перевірте Endpoint IP адресу

### RPi не читає дані з інвертора

1. Перевірте мережу: `ping YOUR_INVERTER_IP`
2. Перевірте порт: `nc -zv YOUR_INVERTER_IP 8000`
3. Перевірте серійні номери в конфізі

### Бот не відповідає

1. Перевірте токен бота
2. Перевірте логи: `journalctl -u luxpower-bot -f`
3. Перевірте з'єднання з RPi: `curl http://10.10.0.2:8080/status`

---

## Наступні кроки

Після успішного встановлення:

1. **Налаштуйте алерти** під свої потреби в `config.py`
2. **Додайте користувачів** до public бота
3. **Налаштуйте графіки** (період, тип)
4. **Створіть бекапи** бази даних

---

## Файли для створення

Наступний крок - створити код для:
- `telegram-bot/rpi/` - код для Raspberry Pi
- `telegram-bot/vps/` - код для VPS

Перейти до створення коду?
