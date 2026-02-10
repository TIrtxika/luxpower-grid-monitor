# LuxPower Monitoring System - Telegram Bot

## Огляд системи

```
┌─────────────────────────────────────────────────────────────────────┐
│                           ІНТЕРНЕТ                                  │
└─────────────────────────────────────────────────────────────────────┘
         │                                    ▲
         ▼                                    │ WireGuard
┌─────────────────┐                 ┌─────────┴───────────────────────┐
│  Telegram API   │◄───────────────►│        VPS (Debian)             │
│                 │                 │  ┌─────────────────────────┐    │
└─────────────────┘                 │  │   WireGuard SERVER      │    │
                                    │  │   wg0: 10.10.0.1/24     │    │
                                    │  │   Listen: 51820/UDP     │    │
                                    │  └───────────┬─────────────┘    │
                                    │              │                  │
                                    │  ┌───────────▼─────────────┐    │
                                    │  │   Telegram Bot Service  │    │
                                    │  │   - Public bot          │    │
                                    │  │   - Private bot         │    │
                                    │  └───────────┬─────────────┘    │
                                    │              │                  │
                                    │  ┌───────────▼─────────────┐    │
                                    │  │   PostgreSQL            │    │
                                    │  │   - Історія даних       │    │
                                    │  │   - Статистика          │    │
                                    │  └─────────────────────────┘    │
                                    └─────────────────────────────────┘
                                                   ▲
                                                   │ WireGuard Tunnel
                                                   │ (RPi ініціює)
┌──────────────────────────────────────────────────┼──────────────────┐
│              ЛОКАЛЬНА МЕРЕЖА YOUR_LOCAL_NETWORK/24      │                  │
│                                                  │                  │
│  ┌───────────────────────────────────────────────┴─────────────┐   │
│  │                     ASUS Router                              │   │
│  │                     Gateway: YOUR_GATEWAY_IP                     │   │
│  └───────────────────────────────┬─────────────────────────────┘   │
│                                  │                                  │
│          ┌───────────────────────┼───────────────────────┐         │
│          │                       │                       │         │
│          ▼                       ▼                       ▼         │
│  ┌───────────────┐    ┌─────────────────────┐    ┌─────────────┐   │
│  │ RPi Zero W 2  │    │  LuxPower Dongle    │    │ Інші        │   │
│  │ YOUR_RPI_LOCAL_IP   │───►│  YOUR_INVERTER_IP:8000 │    │ пристрої    │   │
│  │               │    └─────────────────────┘    └─────────────┘   │
│  │ WireGuard     │              │                                  │
│  │ CLIENT        │              ▼                                  │
│  │ wg0: 10.10.0.2│    ┌─────────────────────┐                      │
│  │               │    │  LuxPower Inverter  │                      │
│  │ Services:     │    │  + Battery + Grid   │                      │
│  │ - Collector   │    └─────────────────────┘                      │
│  │ - REST API    │                                                 │
│  └───────────────┘                                                 │
└─────────────────────────────────────────────────────────────────────┘
```

## Компоненти системи

### 1. Raspberry Pi Zero W 2 (Data Collector)

**Розташування**: Локальна мережа
**IP**: За DHCP або статична

**Функції**:
- Збір даних з інвертора кожні 30 секунд
- REST API сервер (Flask/FastAPI) для VPS
- Локальний кеш останніх даних
- Детектування змін стану мережі

**Сервіси**:
```
luxpower-collector.service  - збір даних
luxpower-api.service        - REST API
```

### 2. VPS (Telegram Bot + DB)

**Розташування**: Інтернет (публічний сервер)

**Функції**:
- Telegram Bot (python-telegram-bot)
- Періодичний опитування RPi через WireGuard
- Збереження історії в БД
- Генерація алертів
- Генерація графіків

**Сервіси**:
```
luxpower-bot.service        - Telegram бот
postgresql.service          - База даних (або SQLite)
```

### 3. WireGuard VPN

**Чому WireGuard**:
- Легкий (менше CPU на RPi)
- Швидкий reconnect
- Простий в налаштуванні

**Топологія**:
- VPS (Debian): WireGuard **Server** (10.10.0.1)
- RPi Zero W 2: WireGuard **Client** (10.10.0.2)
- Локальна мережа RPi: YOUR_LOCAL_NETWORK/24
- Інвертор: YOUR_INVERTER_IP

**Переваги такої топології**:
- RPi ініціює з'єднання (працює за NAT без проблем)
- VPS завжди доступний (публічна IP)
- Простий firewall на роутері (не потрібен port forwarding)

## Потоки даних

### Збір даних (кожні 30 сек)
```
Inverter → WiFi Dongle → RPi Collector → Local Cache
```

### Запит статусу від бота
```
User → Telegram → VPS Bot → WireGuard → RPi API → Response
```

### Алерт про зміну стану
```
RPi Collector → Detect Change → API Callback → VPS → Telegram → User
```

## Telegram боти

### Public Bot (@YourGridStatusBot)

**Доступ**: Всі користувачі
**Команди**:
- `/status` - Статус мережі (є/немає), напруга
- `/history` - Історія відключень за 24 години
- `/subscribe` - Підписка на сповіщення

**Автоматичні сповіщення**:
- "⚡ Мережа відключена (15:30)"
- "✅ Мережа відновлена (15:45). Тривалість: 15 хв"

### Private Bot (@YourInverterBot)

**Доступ**: Тільки авторизовані користувачі (ADMIN_IDS)
**Команди**:
- `/status` - Повний статус (SOC, напруги, потужності, температури)
- `/history` - Історія відключень
- `/graph [period]` - Графік SOC/потужності за період
- `/stats` - Статистика (споживання, відключення)
- `/alerts` - Налаштування алертів

**Автоматичні сповіщення**:
- Зміна стану мережі
- SOC < 20%
- Температура > 60°C
- Втрата зв'язку з інвертором

## База даних (PostgreSQL)

### Таблиці

```sql
-- Поточний стан (кеш)
CREATE TABLE current_status (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    grid_available BOOLEAN,
    grid_voltage REAL,
    battery_soc INTEGER,
    battery_voltage REAL,
    battery_power REAL,
    load_power REAL,
    raw_data JSONB
);

-- Історія станів
CREATE TABLE status_history (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    grid_available BOOLEAN,
    grid_voltage REAL,
    battery_soc INTEGER,
    battery_voltage REAL,
    battery_power REAL,
    load_power REAL
);

-- Події (відключення мережі)
CREATE TABLE grid_events (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(20),  -- 'grid_off', 'grid_on'
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    duration_seconds INTEGER,  -- NULL якщо ще не завершено
    notified BOOLEAN DEFAULT FALSE
);

-- Підписники public бота
CREATE TABLE subscribers (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT UNIQUE,
    subscribed_at TIMESTAMPTZ DEFAULT NOW(),
    active BOOLEAN DEFAULT TRUE
);
```

## Файлова структура

```
telegram-bot/
├── ARCHITECTURE.md           # Цей файл
├── SETUP.md                  # Інструкція з встановлення
├── README.md                 # Короткий опис
│
├── rpi/                      # Код для Raspberry Pi
│   ├── collector.py          # Збір даних з інвертора
│   ├── api.py                # REST API сервер
│   ├── config.py             # Конфігурація
│   ├── requirements.txt
│   └── systemd/
│       ├── luxpower-collector.service
│       └── luxpower-api.service
│
├── vps/                      # Код для VPS
│   ├── bot.py                # Telegram бот
│   ├── database.py           # Робота з БД
│   ├── poller.py             # Опитування RPi
│   ├── alerts.py             # Система алертів
│   ├── graphs.py             # Генерація графіків
│   ├── config.py             # Конфігурація
│   ├── requirements.txt
│   └── systemd/
│       └── luxpower-bot.service
│
└── wireguard/                # Конфіги WireGuard
    ├── wg0-server.conf       # Конфіг для VPS (server)
    └── wg0-client.conf       # Конфіг для RPi (client)
```

## Безпека

1. **WireGuard**: Шифрований тунель між VPS і локальною мережею
2. **API Authentication**: Bearer token для REST API
3. **Telegram**: Перевірка ADMIN_IDS для приватного бота
4. **Firewall**:
   - VPS: Тільки WireGuard + SSH
   - RPi API: Тільки з WireGuard мережі

## Порти

| Компонент | Порт | Протокол | Опис |
|-----------|------|----------|------|
| WireGuard | 51820 | UDP | VPN тунель |
| RPi API | 8080 | TCP | REST API (тільки WireGuard) |
| Inverter | 8000 | TCP | LuxPower протокол |
| PostgreSQL | 5432 | TCP | База даних (localhost) |

## Етапи реалізації

### Етап 1: WireGuard VPN
1. Налаштування WireGuard Server на VPS (Debian)
2. Налаштування WireGuard Client на RPi
3. Перевірка з'єднання VPS ↔ RPi
4. Перевірка доступу до інвертора через тунель

### Етап 2: Raspberry Pi (Data Collector)
1. Встановлення Python та залежностей
2. Деплой collector сервісу
3. Деплой REST API сервісу
4. Налаштування systemd сервісів
5. Тестування локально та через WireGuard

### Етап 3: VPS (Bot + Database)
1. Встановлення PostgreSQL
2. Створення бази даних та таблиць
3. Деплой Telegram бота
4. Налаштування systemd сервісу
5. Створення ботів у @BotFather

### Етап 4: Інтеграція та тестування
1. End-to-end тестування
2. Налаштування алертів
3. Тестування сповіщень
4. Моніторинг та логування

## Наступні кроки

Перейти до файлу `SETUP.md` для покрокової інструкції встановлення.
