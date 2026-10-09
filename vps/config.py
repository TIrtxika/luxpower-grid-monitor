"""
Конфігурація для LuxPower Telegram Bot
VPS сервер
"""
import os

# =============================================================================
# TELEGRAM BOTS
# =============================================================================
# Public bot - для всіх (статус мережі, історія)
PUBLIC_BOT_TOKEN = os.environ.get("PUBLIC_BOT_TOKEN", "YOUR_PUBLIC_BOT_TOKEN")

# Private bot - для власника (повний статус, графіки, налаштування)
PRIVATE_BOT_TOKEN = os.environ.get("PRIVATE_BOT_TOKEN", "YOUR_PRIVATE_BOT_TOKEN")

# ID власника для private бота
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0"))

# =============================================================================
# RPi API
# =============================================================================
RPI_API_URL = os.environ.get("RPI_API_URL", "http://10.10.0.2:8080")
RPI_API_TOKEN = os.environ.get("RPI_API_TOKEN", "CHANGE_ME_TO_RANDOM_TOKEN")

# =============================================================================
# DATABASE
# =============================================================================
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://luxpower:YOUR_DB_PASSWORD@localhost:5432/luxpower"
)

# =============================================================================
# POLLER
# =============================================================================
POLL_INTERVAL = 60  # Інтервал опитування RPi (секунди)
STORE_INTERVAL = 300  # Інтервал збереження в БД (секунди) - кожні 5 хвилин

# =============================================================================
# ALERTS
# =============================================================================
# Debounce для зміни стану мережі на стороні VPS (секунди)
# Стан має бути стабільним протягом цього часу перед відправкою алерту
# З POLL_INTERVAL=60 значення 120 означає мінімум 2 послідовних polls
GRID_STATE_DEBOUNCE = 120

# Кількість послідовних помилок зв'язку з RPi перед алертом
# З POLL_INTERVAL=60 значення 3 = RPi недоступний ~3 хвилини
RPI_UNREACHABLE_THRESHOLD = 3

# Дані RPi старші за це (секунди) вважаються застарілими -> стан "невідомо"
STALE_DATA_SECONDS = int(os.environ.get("STALE_DATA_SECONDS", "180"))

# Через скільки секунд стану "невідомо" надіслати алерт власнику
UNKNOWN_ALERT_AFTER = int(os.environ.get("UNKNOWN_ALERT_AFTER", "300"))

# Скільки днів зберігати зразки inverter_status
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))

# SOC, на якому інвертор вважає батарею порожньою (для прогнозу)
BATTERY_EMPTY_SOC = int(os.environ.get("BATTERY_EMPTY_SOC", "10"))

# Пороги SOC (%) для попереджень власнику під час відключення
BATTERY_ALERT_LEVELS = tuple(
    int(x) for x in os.environ.get("BATTERY_ALERT_LEVELS", "30,15").split(",")
    if x.strip()
)

# =============================================================================
# WATCHDOG — зовнішній сторож (healthchecks.io), порожній = вимкнено
# =============================================================================
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

# Канал для публічних сповіщень (опціонально)
PUBLIC_CHANNEL_ID = os.environ.get("PUBLIC_CHANNEL_ID", "")

# =============================================================================
# NTFY — сповіщення власнику поза Telegram
# =============================================================================
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
# Секретний топік; порожній = ntfy вимкнено
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
# Токен доступу (опціонально, для захищених топіків/self-hosted)
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

# =============================================================================
# LOGGING
# =============================================================================
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
