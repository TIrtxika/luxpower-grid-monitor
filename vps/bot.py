#!/usr/bin/env python3
"""
LuxPower Telegram Bot
Main entry point for bot services
"""

import logging
import sys
import asyncio
from datetime import datetime, timezone, timedelta

import requests

# Kyiv timezone (UTC+2, or UTC+3 in summer)
KYIV_TZ = timezone(timedelta(hours=2))


def kyiv_now() -> datetime:
    """Get current time in Kyiv timezone"""
    return datetime.now(KYIV_TZ)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

import config
from database import get_db, close_db
from poller import get_poller
from alerts import AlertManager
import graphs

# Logging setup
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def format_duration(delta: timedelta) -> str:
    """Format duration: minutes -> hours -> days (>7d)"""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds} сек"

    total_minutes = total_seconds // 60
    days = total_seconds // 86400

    if days >= 7:
        return f"{days} дн."

    total_hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    if total_hours >= 1:
        if minutes > 0:
            return f"{total_hours} год {minutes} хв"
        return f"{total_hours} год"

    return f"{total_minutes} хв"


# =============================================================================
# PUBLIC BOT HANDLERS
# =============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    chat_id = update.effective_chat.id

    welcome = (
        f"Привіт, {user.first_name}!\n\n"
        "Цей бот показує стан електромережі.\n\n"
        "Команди:\n"
        "/status - Поточний стан\n"
        "/history - Історія відключень\n"
        "/subscribe - Підписатися на сповіщення\n"
        "/unsubscribe - Відписатися\n"
        "/help - Допомога"
    )

    await update.message.reply_text(welcome)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "Доступні команди:\n\n"
        "/status - Показати поточний стан мережі\n"
        "/history - Історія відключень за 24 години\n"
        "/subscribe - Підписатися на сповіщення про відключення\n"
        "/unsubscribe - Відписатися від сповіщень\n\n"
        "Бот автоматично надсилає повідомлення при зміні стану мережі."
    )
    await update.message.reply_text(help_text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    poller = get_poller()
    status = poller.get_last_status()

    if not status:
        await update.message.reply_text(
            "\u26a0 Дані недоступні. Спробуйте пізніше."
        )
        return

    grid = status.get('grid', {})
    grid_available = grid.get('available', False)
    voltage = grid.get('voltage', 0)

    if grid_available:
        emoji = "\u2705"
        state_text = "УВІМКНЕНО"
    else:
        emoji = "\u274c"
        state_text = "ВИМКНЕНО"

    message = f"{emoji} Електромережа: {state_text}\n"
    message += f"Напруга: {voltage}V"

    # Показати з якого часу поточний стан та тривалість
    try:
        db = get_db()
        event_type = 'grid_on' if grid_available else 'grid_off'
        events = db.get_events(event_type=event_type, hours=24 * 365, limit=1)
        if events:
            event_time = events[0]['timestamp']
            now = kyiv_now()
            # Конвертуємо в Kyiv TZ для відображення
            event_time_kyiv = event_time.astimezone(KYIV_TZ)
            duration = now - event_time_kyiv

            # Формат часу: HH:MM якщо сьогодні, HH:MM DD.MM якщо інший день
            if event_time_kyiv.date() == now.date():
                time_str = event_time_kyiv.strftime('%H:%M')
            else:
                time_str = event_time_kyiv.strftime('%H:%M %d.%m')

            duration_str = format_duration(duration)
            message += f"\nЗ {time_str} ({duration_str})"
    except Exception as e:
        logger.warning(f"Failed to get grid event for status: {e}")

    message += f"\n\nОновлено: {kyiv_now().strftime('%H:%M:%S')}"

    if not poller.is_rpi_available():
        message += "\n\n\u26a0\ufe0f RPi недоступний, дані можуть бути застарілими"

    await update.message.reply_text(message)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command"""
    db = get_db()
    stats = db.get_grid_statistics(hours=24)

    count = stats['outage_count']
    duration = stats['total_duration_minutes']

    if count == 0:
        message = "\u2705 За останні 24 години відключень не було!"
    else:
        message = (
            f"\U0001f4ca Статистика за 24 години:\n\n"
            f"Відключень: {count}\n"
            f"Загальний час без світла: {duration} хв"
        )

    # Add button to get detailed history
    keyboard = [[
        InlineKeyboardButton("Детальніше", callback_data="history_detail")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(message, reply_markup=reply_markup)


async def callback_history_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle history detail callback"""
    query = update.callback_query
    await query.answer()

    db = get_db()
    events = db.get_events(hours=24)

    grid_events = [e for e in events if e['event_type'] in ('grid_on', 'grid_off')]

    if not grid_events:
        await query.edit_message_text("Подій немає.")
        return

    message = "\U0001f4cb Історія подій:\n\n"

    for event in grid_events[-10:]:  # Last 10 events
        ts = event['timestamp'].strftime('%H:%M %d.%m')
        if event['event_type'] == 'grid_off':
            message += f"\u274c {ts} - Відключено\n"
        else:
            duration = event.get('data', {}).get('duration_seconds', 0)
            if duration:
                mins = duration // 60
                message += f"\u2705 {ts} - Увімкнено (було вимкнено {mins} хв)\n"
            else:
                message += f"\u2705 {ts} - Увімкнено\n"

    await query.edit_message_text(message)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscribe command"""
    chat_id = update.effective_chat.id
    username = update.effective_user.username

    db = get_db()

    if db.is_subscribed(chat_id):
        await update.message.reply_text("Ви вже підписані на сповіщення.")
        return

    db.add_subscriber(chat_id, username)
    await update.message.reply_text(
        "\u2705 Ви підписались на сповіщення!\n"
        "Ви будете отримувати повідомлення при зміні стану мережі."
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unsubscribe command"""
    chat_id = update.effective_chat.id

    db = get_db()

    if not db.is_subscribed(chat_id):
        await update.message.reply_text("Ви не підписані на сповіщення.")
        return

    db.remove_subscriber(chat_id)
    await update.message.reply_text(
        "\u274c Ви відписались від сповіщень."
    )


# =============================================================================
# PRIVATE BOT HANDLERS (Owner only)
# =============================================================================

def owner_only(func):
    """Decorator to restrict command to owner only"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != config.OWNER_CHAT_ID:
            await update.message.reply_text("Доступ заборонено.")
            return
        return await func(update, context)
    return wrapper


def fetch_status_direct() -> dict:
    """Fetch status directly from RPi API"""
    try:
        response = requests.get(
            f"{config.RPI_API_URL}/status",
            headers={"Authorization": f"Bearer {config.RPI_API_TOKEN}"},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch status: {e}")
        return None


@owner_only
async def cmd_full_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /fullstatus command (private bot)"""
    # Try poller first, then fetch directly
    poller = get_poller()
    status = poller.get_last_status()

    if not status:
        status = fetch_status_direct()

    if not status:
        await update.message.reply_text("\u26a0 Дані недоступні")
        return

    grid = status.get('grid', {})
    battery = status.get('battery', {})
    output = status.get('output', {})
    temp = status.get('temperature', {})

    grid_emoji = "\u2705" if grid.get('available') else "\u274c"

    message = (
        f"\U0001f50b Повний статус інвертора\n\n"
        f"{grid_emoji} Мережа: {grid.get('voltage', 0)}V / {grid.get('frequency', 0)}Hz\n\n"
        f"\U0001faab Батарея: {battery.get('soc', 0)}%\n"
        f"   Напруга: {battery.get('voltage', 0)}V\n"
        f"   Струм: {battery.get('current', 0)}A\n"
        f"   Потужність: {battery.get('power', 0)}W\n\n"
        f"\U0001f3e0 Навантаження: {output.get('load_power', 0)}W\n"
        f"   Вихід: {output.get('voltage', 0)}V / {output.get('frequency', 0)}Hz\n\n"
        f"\U0001f321 Температура:\n"
        f"   Інвертор: {temp.get('inverter', 0)}°C\n"
        f"   Радіатор: {temp.get('radiator', 0)}°C\n\n"
        f"DC Bus: {status.get('dc_bus_voltage', 0)}V"
    )

    await update.message.reply_text(message)


@owner_only
async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /chart command (private bot)"""
    keyboard = [
        [InlineKeyboardButton("Напруга", callback_data="chart_voltage")],
        [InlineKeyboardButton("Батарея", callback_data="chart_battery")],
        [InlineKeyboardButton("Навантаження", callback_data="chart_load")],
        [InlineKeyboardButton("Все разом", callback_data="chart_combined")],
        [InlineKeyboardButton("Відключення", callback_data="chart_outages")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Виберіть тип графіка:",
        reply_markup=reply_markup
    )


async def callback_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle chart callbacks"""
    query = update.callback_query

    if query.from_user.id != config.OWNER_CHAT_ID:
        await query.answer("Доступ заборонено", show_alert=True)
        return

    await query.answer("Генерую графік...")

    chart_type = query.data.replace("chart_", "")

    chart_funcs = {
        "voltage": graphs.create_voltage_chart,
        "battery": graphs.create_battery_chart,
        "load": graphs.create_load_chart,
        "combined": graphs.create_combined_chart,
        "outages": graphs.create_outage_timeline
    }

    func = chart_funcs.get(chart_type)
    if not func:
        await query.edit_message_text("Невідомий тип графіка")
        return

    chart_data = func(hours=24)

    if chart_data:
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=chart_data,
            caption=f"Графік за останні 24 години"
        )
        await query.delete_message()
    else:
        await query.edit_message_text("Недостатньо даних для графіка")


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command (private bot)"""
    db = get_db()

    # Get statistics for different periods
    stats_24h = db.get_grid_statistics(hours=24)
    stats_7d = db.get_grid_statistics(hours=168)

    message = (
        "\U0001f4ca Статистика відключень\n\n"
        "За 24 години:\n"
        f"  Відключень: {stats_24h['outage_count']}\n"
        f"  Загалом: {stats_24h['total_duration_minutes']} хв\n\n"
        "За 7 днів:\n"
        f"  Відключень: {stats_7d['outage_count']}\n"
        f"  Загалом: {stats_7d['total_duration_minutes']} хв"
    )

    await update.message.reply_text(message)


@owner_only
async def cmd_subscribers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscribers command (private bot)"""
    db = get_db()
    subscribers = db.get_active_subscribers()

    await update.message.reply_text(
        f"\U0001f465 Підписників: {len(subscribers)}"
    )


# =============================================================================
# MAIN
# =============================================================================

def run_public_bot():
    """Run public bot only"""
    logger.info("Starting public bot...")

    # Initialize database
    db = get_db()

    # Initialize poller
    poller = get_poller()
    poller.start()

    # Create application
    app = Application.builder().token(config.PUBLIC_BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))

    app.add_handler(CallbackQueryHandler(callback_history_detail, pattern="^history_"))

    # Setup alerts
    alert_manager = AlertManager(app.bot)
    alert_manager.set_event_loop(asyncio.get_event_loop())
    poller.add_state_callback(alert_manager.on_grid_state_change)
    poller.add_rpi_callback(alert_manager.on_rpi_state_change)

    logger.info("Public bot started")

    # Run
    app.run_polling(allowed_updates=Update.ALL_TYPES)

    # Cleanup
    poller.stop()
    close_db()


def run_private_bot():
    """Run private bot only"""
    logger.info("Starting private bot...")

    # Create application
    app = Application.builder().token(config.PRIVATE_BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_full_status))
    app.add_handler(CommandHandler("fullstatus", cmd_full_status))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("subscribers", cmd_subscribers))

    app.add_handler(CallbackQueryHandler(callback_chart, pattern="^chart_"))

    logger.info("Private bot started")

    # Run
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def run_both_bots():
    """Run both bots (for testing)"""
    import threading

    # Start public bot in separate thread
    public_thread = threading.Thread(target=run_public_bot, daemon=True)
    public_thread.start()

    # Run private bot in main thread
    run_private_bot()


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="LuxPower Telegram Bot")
    parser.add_argument(
        "--bot", choices=["public", "private", "both"],
        default="public",
        help="Which bot to run"
    )
    args = parser.parse_args()

    if args.bot == "public":
        run_public_bot()
    elif args.bot == "private":
        run_private_bot()
    else:
        run_both_bots()


if __name__ == "__main__":
    main()
