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
| `STALE_DATA_SECONDS` | 180 | RPi data older than this → state "unknown" (🟡) |
| `UNKNOWN_ALERT_AFTER` | 300 | Seconds of "unknown" before the owner is alerted |
| `RETENTION_DAYS` | 90 | Days of `inverter_status` samples to keep |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server for owner alerts outside Telegram |
| `NTFY_TOPIC` | — | Secret ntfy topic (empty = ntfy disabled); test with `/ntfytest` in the private bot |
| `NTFY_TOKEN` | — | Optional ntfy access token (sent as `Authorization: Bearer`) |
| `BATTERY_EMPTY_SOC` | 10 | SOC (%) treated as empty for the runtime forecast |
| `BATTERY_ALERT_LEVELS` | `30,15` | SOC thresholds for owner warnings during an outage (the lowest is urgent) |
| `HEALTHCHECK_URL` | — | healthchecks.io ping URL; the poller pings it every poll so you are alerted if the VPS or the bot goes silent (empty = disabled) |

### Grid state history (`grid_intervals`)

The poller records the grid state as intervals (`on` / `off` / `unknown`).
Statistics in `/grid`, `/history`, `/stats` are computed from them; time when
the monitor had no data is shown as "unknown" and excluded from availability %.

One-off backfill from existing samples (run with the public bot stopped):

    python migrate_intervals.py --dry-run
    python migrate_intervals.py

## Operations

### Tests (CI)

GitHub Actions (`.github/workflows/tests.yml`) runs the whole `vps/` test suite on
Python 3.11 with a PostgreSQL 15 service for every pull request and push to `main`.
Locally: `cd vps && TEST_DATABASE_URL=postgresql://... python -m unittest discover -s . -p 'test_*.py'`
(DB integration tests are skipped without `TEST_DATABASE_URL`).

### Deploy (`vps/ops/deploy.sh`)

On the dev machine, make a checksum manifest of the commit to deploy:

    for f in $(git ls-tree --name-only <commit> vps/ | grep -E '\.py$|requirements.txt' | grep -v '/test_'); do
        printf '%s  %s\n' "$(git show <commit>:$f | sha256sum | cut -d' ' -f1)" "$(basename $f)"
    done > manifest.sha256

Copy the manifest to the VPS and run:

    sudo /usr/local/sbin/luxpower-deploy <commit> /tmp/manifest.sha256

The script downloads the commit from GitHub, refuses to deploy if any file does not
match the manifest, compiles the code, backs up `/opt/luxpower` code to
`/var/backups/luxpower/code-<timestamp>`, installs, restarts both bots and fails
(printing the rollback command) on inactive units, tracebacks, `[ERROR]` lines or
bot-token URLs in the logs.

### Database backups

`luxpower-db-backup.timer` runs `vps/ops/backup-db.sh` daily at 03:00 Kyiv time
(`Persistent=true` catches up after downtime): gzipped `pg_dump` into
`/var/backups/luxpower/daily/` (mode 600), newest 14 kept.

    # root-owned location: never under /opt/luxpower, which the bot user owns
    sudo install -o root -g root -m 755 vps/ops/backup-db.sh /usr/local/sbin/luxpower-backup-db
    sudo install -o root -g root -m 755 vps/ops/deploy.sh /usr/local/sbin/luxpower-deploy
    sudo cp vps/systemd/luxpower-db-backup.* /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now luxpower-db-backup.timer

Restore: `gunzip -c <dump>.sql.gz | sudo -u postgres psql luxpower` (into an empty DB).

## Telegram Commands

### Public Bot

| Command | Description |
|---------|-------------|
| `/status` | Current grid state, voltage, duration |
| `/grid` | Grid availability stats (daily/weekly/monthly with visual bars) |
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
