#!/usr/bin/env python3
"""
LuxPower Telegram Bot
Main entry point for bot services
"""

import logging
import sys
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# Kyiv timezone (EET/EEST, follows DST)
KYIV_TZ = ZoneInfo("Europe/Kyiv")


def kyiv_now() -> datetime:
    """Get current time in Kyiv timezone"""
    return datetime.now(KYIV_TZ)

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

import config
from database import get_db, close_db
from poller import get_poller
from alerts import AlertManager, UNKNOWN_REASONS_UA
from grid_state import ON, OFF
import stats as grid_stats
from messages import (
    format_history_summary, format_outages, format_periods, format_since,
    format_stats,
)
import graphs

# Logging setup
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# httpx logs full Telegram URLs (with the bot token) at INFO
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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
        "/grid - Статистика наявності світла\n"
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
        "/status - Поточний стан мережі\n"
        "/grid - Статистика наявності світла\n"
        "/history - Історія відключень за 24 години\n"
        "/subscribe - Підписатися на сповіщення\n"
        "/unsubscribe - Відписатися від сповіщень\n\n"
        "Бот автоматично надсилає повідомлення при зміні стану мережі."
    )
    await update.message.reply_text(help_text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    poller = get_poller()
    status = poller.get_last_status()
    state = poller.get_effective_state()

    if state is None:
        await update.message.reply_text(
            "⚠ Дані недоступні. Спробуйте пізніше."
        )
        return

    voltage = ((status or {}).get('grid') or {}).get('voltage', 0)
    if state == ON:
        message = f"✅ Електромережа: УВІМКНЕНО\nНапруга: {voltage}V"
    elif state == OFF:
        message = f"❌ Електромережа: ВИМКНЕНО\nНапруга: {voltage}V"
    else:
        reason = UNKNOWN_REASONS_UA.get(poller.get_state_reason(),
                                        "невідома причина")
        message = f"⚠️ Немає свіжих даних про мережу\nПричина: {reason}"

    try:
        open_iv = get_db().get_open_interval()
        if open_iv:
            message += "\n" + format_since(open_iv['started_at'],
                                           datetime.now(timezone.utc))
    except Exception as e:
        logger.warning(f"Failed to get open interval for status: {e}")

    message += f"\n\nОновлено: {kyiv_now().strftime('%H:%M:%S')}"
    await update.message.reply_text(message)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command: last 24 hours"""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    intervals = get_db().get_intervals(start, now)
    message = format_history_summary(
        grid_stats.window_stats(intervals, start, now),
        grid_stats.outages(intervals, start, now),
    )

    keyboard = [[
        InlineKeyboardButton("Детальніше", callback_data="history_detail")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(message, reply_markup=reply_markup)


async def callback_history_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle history detail callback: outages of the last 7 days"""
    query = update.callback_query
    await query.answer()

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    intervals = get_db().get_intervals(start, now)
    await query.edit_message_text(
        format_outages(grid_stats.outages(intervals, start, now))
    )


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
# GRID AVAILABILITY STATS
# =============================================================================

# Button period -> number of periods shown
GRID_VIEWS = {'day': 7, 'week': 5, 'month': 12}


def _grid_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Тиждень", callback_data="grid_day"),
        InlineKeyboardButton("Місяць", callback_data="grid_week"),
        InlineKeyboardButton("Рік", callback_data="grid_month"),
    ]])


def _grid_message(period: str) -> str:
    count = GRID_VIEWS[period]
    now = datetime.now(timezone.utc)
    first_start = grid_stats.period_bounds(period, count, now)[0][0]
    intervals = get_db().get_intervals(first_start, now)
    periods = grid_stats.split_by_periods(intervals, period, count, now)
    return format_periods(periods, period)


async def cmd_grid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /grid command — grid availability statistics"""
    await update.message.reply_text(_grid_message('day'),
                                    reply_markup=_grid_keyboard())


async def callback_grid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle grid view switching"""
    query = update.callback_query
    await query.answer()

    period = query.data.replace("grid_", "")
    if period not in GRID_VIEWS:
        return
    await query.edit_message_text(_grid_message(period),
                                  reply_markup=_grid_keyboard())


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
    now = datetime.now(timezone.utc)
    windows = [("За 24 години", timedelta(hours=24)),
               ("За 7 днів", timedelta(days=7)),
               ("За 30 днів", timedelta(days=30))]
    intervals = get_db().get_intervals(now - windows[-1][1], now)

    rows = []
    for label, length in windows:
        start = now - length
        rows.append((label,
                     grid_stats.window_stats(intervals, start, now),
                     grid_stats.outage_summary(
                         grid_stats.outages(intervals, start, now))))

    await update.message.reply_text(format_stats(rows))


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
    get_db()
    poller = get_poller()
    private_bot = Bot(config.PRIVATE_BOT_TOKEN)

    async def on_start(application: Application):
        """Wire alerts and start polling once the bot loop is running"""
        owner_bot = private_bot
        try:
            await private_bot.initialize()
        except Exception as e:
            logger.error(f"Private bot unavailable for owner alerts: {e}")
            owner_bot = None

        alert_manager = AlertManager(application.bot, owner_bot)
        alert_manager.set_event_loop(asyncio.get_running_loop())
        poller.add_state_callback(alert_manager.on_grid_change)
        poller.add_unknown_callback(alert_manager.on_unknown_change)
        poller.start()

    async def on_stop(application: Application):
        poller.stop()
        try:
            await private_bot.shutdown()
        except Exception as e:
            logger.warning(f"Private bot shutdown: {e}")

    app = (Application.builder()
           .token(config.PUBLIC_BOT_TOKEN)
           .post_init(on_start)
           .post_shutdown(on_stop)
           .build())

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("grid", cmd_grid))

    app.add_handler(CallbackQueryHandler(callback_history_detail, pattern="^history_"))
    app.add_handler(CallbackQueryHandler(callback_grid, pattern="^grid_"))

    logger.info("Public bot started")

    # Run
    app.run_polling(allowed_updates=Update.ALL_TYPES)

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
