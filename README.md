# LuxPower Grid Monitor

Real-time grid power monitoring system for LuxPower inverters with Telegram notifications.

## Architecture

```
                    Internet
                       |
              +--------+--------+
              |   VPS (Debian)  |
              |  Telegram Bots  |
              |   PostgreSQL    |
              +--------+--------+
                       |
                  WireGuard VPN
                       |
              +--------+--------+
              | Raspberry Pi    |
              | Data Collector  |
              | REST API        |
              +--------+--------+
                       |
              +--------+--------+
              | LuxPower        |
              | Inverter+Dongle |
              +-----------------+
```

**Raspberry Pi** collects data from the inverter every 30 seconds via proprietary protocol over TCP and exposes it through a REST API.

**VPS** polls the RPi API through a WireGuard tunnel, detects grid state changes with debounce, stores history in PostgreSQL, and sends Telegram alerts.

## Features

- Grid outage/recovery detection with configurable debounce (avoids false triggers)
- Telegram notifications to subscribers (public bot) and detailed status to owner (private bot)
- RPi connectivity monitoring with alerts when the collector goes offline
- Historical statistics and outage duration tracking
- `/status` command shows current state, voltage, and how long the grid has been on/off
- `/history` command with outage statistics for 24h / 7d periods
- Charts generation (voltage, battery SOC, load power)

## Components

| Directory | Runs on | Description |
|-----------|---------|-------------|
| `rpi/` | Raspberry Pi | Inverter data collector + REST API |
| `vps/` | VPS | Telegram bots + PostgreSQL + poller |
| `wireguard/` | Both | WireGuard VPN config templates |

## Quick Start

### 1. WireGuard VPN

See [`wireguard/README.md`](wireguard/README.md) for key generation and setup.

### 2. Raspberry Pi

```bash
# Install
mkdir -p ~/luxpower && cd ~/luxpower
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure
cp config.py config.py.bak
nano config.py  # Set INVERTER_IP, DONGLE_SERIAL, INVERTER_SERIAL, API_TOKEN

# Deploy services
sudo cp systemd/*.service /etc/systemd/system/
# Edit User/WorkingDirectory in service files to match your setup
sudo systemctl daemon-reload
sudo systemctl enable --now luxpower-collector luxpower-api
```

### 3. VPS

```bash
# Install
mkdir -p ~/bot && cd ~/bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env  # Set bot tokens, DB credentials, RPi API URL/token

# Database (auto-creates tables on first run)
sudo -u postgres createuser luxpower
sudo -u postgres createdb -O luxpower luxpower

# Deploy services
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now luxpower-bot-public luxpower-bot-private
```

### 4. Create Telegram Bots

1. Talk to [@BotFather](https://t.me/BotFather), create two bots (public + private)
2. Set tokens in `.env`
3. Get your Telegram ID from [@userinfobot](https://t.me/userinfobot), set as `OWNER_CHAT_ID`

## Configuration

### RPi (`rpi/config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `INVERTER_IP` | — | WiFi dongle IP on local network |
| `DONGLE_SERIAL` | — | 10-char dongle serial (from label) |
| `INVERTER_SERIAL` | — | 10-char inverter serial |
| `POLL_INTERVAL` | 30s | How often to read inverter data |
| `GRID_VOLTAGE_THRESHOLD` | 180V | Below this = grid is OFF |
| `STATE_CHANGE_DEBOUNCE` | 10s | Local debounce for state changes |

### VPS (`vps/.env`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `PUBLIC_BOT_TOKEN` | — | Telegram public bot token |
| `PRIVATE_BOT_TOKEN` | — | Telegram private bot token |
| `OWNER_CHAT_ID` | — | Your Telegram user ID |
| `RPI_API_URL` | `http://10.10.0.2:8080` | RPi API via WireGuard |
| `DATABASE_URL` | — | PostgreSQL connection string |
| `GRID_STATE_DEBOUNCE` | 120s | VPS-side debounce (filters false triggers) |
| `RPI_UNREACHABLE_THRESHOLD` | 3 | Consecutive failures before RPi alert |

## Telegram Commands

### Public Bot

| Command | Description |
|---------|-------------|
| `/status` | Current grid state, voltage, duration |
| `/history` | Outage statistics for last 24 hours |
| `/subscribe` | Enable outage notifications |
| `/unsubscribe` | Disable notifications |

### Private Bot (owner only)

| Command | Description |
|---------|-------------|
| `/fullstatus` | Full inverter status (SOC, temps, power) |
| `/chart` | Generate voltage/battery/load charts |
| `/stats` | Outage statistics (24h + 7d) |
| `/subscribers` | Subscriber count |

## Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — System design and data flows
- [`SETUP.md`](SETUP.md) — Step-by-step installation guide
- [`LUXPOWER_PROTOCOL.md`](LUXPOWER_PROTOCOL.md) — Inverter protocol reverse-engineering docs

## License

Private project.
