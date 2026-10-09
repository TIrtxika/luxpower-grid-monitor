#!/usr/bin/env python3
"""
LuxPower Telegram Bot
Main entry point for bot services
"""

import functools
import logging
import sys
import time
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg2
import requests
from telegram.error import TelegramError

# Kyiv timezone (EET/EEST, follows DST)
KYIV_TZ = ZoneInfo("Europe/Kyiv")


def kyiv_now() -> datetime:
    """Get current time in Kyiv timezone"""
    return datetime.now(KYIV_TZ)

from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

import config
from database import get_db, close_db
from poller import get_poller
from alerts import AlertManager, UNKNOWN_REASONS_UA
from grid_state import UNKNOWN
import stats as grid_stats
import menus
from messages import (
    format_history_summary, format_inverter_details, format_outages,
    format_battery_line, format_periods, format_plan_fact, format_settings,
    format_stats, format_traffic_light,
    status_from_sample,
)
from battery import forecast as battery_forecast
import schedule as dtek_schedule
from subscriptions import MODES, QUIET_WINDOWS
import charts
from notifiers import Notice, ntfy_from_config

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

# Rendered charts, shared by all chats of this process
CHART_CACHE = charts.ChartCache()

DB_DOWN = "⚠ База даних тимчасово недоступна, спробуйте пізніше"


def db_guarded(func):
    """Answer the user instead of staying silent when the DB is unreachable"""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except psycopg2.Error as e:
            logger.error(f"DB unavailable in {func.__name__}: {type(e).__name__}: {e}")
            query = update.callback_query
            if query is None:
                await update.message.reply_text(DB_DOWN)
                return
            try:
                await query.answer(DB_DOWN, show_alert=True)
            except TelegramError:
                # the query was already answered before the DB call
                await query.message.reply_text(DB_DOWN)
    return wrapper


# =============================================================================
# PUBLIC BOT HANDLERS
# =============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user

    welcome = (
        f"Привіт, {user.first_name}!\n\n"
        "Цей бот показує стан електромережі.\n"
        "Користуйтесь кнопками внизу або командами:\n\n"
        "/status - Поточний стан\n"
        "/grid - Статистика наявності світла\n"
        "/history - Історія відключень\n"
        "/subscribe - Підписатися на сповіщення\n"
        "/unsubscribe - Відписатися\n"
        "/help - Допомога"
    )

    await update.message.reply_text(welcome,
                                    reply_markup=menus.public_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "Доступні команди:\n\n"
        "/status - Поточний стан мережі\n"
        "/grid - Статистика наявності світла\n"
        "/history - Історія відключень за 24 години\n"
        "/subscribe - Підписатися на сповіщення\n"
        "/unsubscribe - Відписатися від сповіщень\n"
        "/settings - Тихі години (за замовчуванням 23:00–07:00 беззвучно) "
        "та які сповіщення надсилати, нагадування за графіком ДТЕК\n\n"
        "\U0001f7e2 є світло  \U0001f534 немає  \U0001f7e1 невідомо\n"
        "Бот автоматично надсилає повідомлення при зміні стану мережі."
    )
    await update.message.reply_text(help_text,
                                    reply_markup=menus.public_keyboard())


def _schedule_line(now: datetime, watch=None) -> Optional[str]:
    """📅 line of /status: poller memory (public bot) or the DB"""
    if not config.DTEK_GROUP:
        return None
    days = watch.days(now) if watch is not None else []
    if not days:
        today = now.astimezone(KYIV_TZ).date()
        try:
            days = get_db().get_schedule(config.DTEK_GROUP, today,
                                         today + timedelta(days=1))
        except Exception as e:
            logger.warning(f"Failed to load schedule: {type(e).__name__}")
            return None
    return dtek_schedule.describe(days, now)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command: traffic light 🟢 / 🔴 / 🟡"""
    poller = get_poller()
    status = poller.get_last_status() or {}
    state = poller.get_effective_state()

    if state is None:
        await update.message.reply_text(
            "⚠ Дані недоступні. Спробуйте пізніше."
        )
        return

    now = datetime.now(timezone.utc)
    # In-memory state: stays right even if a write to the DB failed
    since = poller.get_state_since()

    reason = None
    if state == UNKNOWN:
        reason = UNKNOWN_REASONS_UA.get(poller.get_state_reason(),
                                        "невідома причина")
    data_ts = status.get('timestamp')
    data_age = time.time() - data_ts if data_ts else None
    voltage = (status.get('grid') or {}).get('voltage')

    message = format_traffic_light(state, reason, since, now,
                                   voltage=voltage, data_age_s=data_age)
    schedule_line = _schedule_line(now, poller.schedule)
    if schedule_line:
        message += "\n" + schedule_line
    message += f"\n\nОновлено: {kyiv_now().strftime('%H:%M:%S')}"
    await update.message.reply_text(message)


NOT_SUBSCRIBED = "Спершу підпишіться на сповіщення: /subscribe"


@db_guarded
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings: quiet hours and which alerts to send"""
    settings = get_db().get_settings(update.effective_chat.id)
    if settings is None:
        await update.message.reply_text(NOT_SUBSCRIBED)
        return
    await update.message.reply_text(format_settings(settings),
                                    reply_markup=menus.settings_keyboard(settings))


def _settings_change(data: str) -> Optional[dict]:
    """'set:<kind>:<value>' -> fields for update_settings, None if invalid"""
    parts = (data or '').split(':')
    if len(parts) != 3 or parts[0] != 'set':
        return None
    _, kind, value = parts
    if kind == 'quiet' and value in ('on', 'off'):
        return {'quiet_enabled': value == 'on'}
    if kind == 'window' and value in QUIET_WINDOWS:
        start, end = QUIET_WINDOWS[value]
        return {'quiet_from': start, 'quiet_to': end}
    if kind == 'mode' and value in MODES:
        return {'notify_mode': value}
    if kind == 'remind' and value in ('on', 'off'):
        return {'remind_enabled': value == 'on'}
    return None


@db_guarded
async def callback_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settings buttons"""
    query = update.callback_query
    chat_id = query.message.chat.id
    db = get_db()
    if db.get_settings(chat_id) is None:
        await query.answer(NOT_SUBSCRIBED, show_alert=True)
        return
    change = _settings_change(query.data)
    if change is None:
        await query.answer()
        return
    db.update_settings(chat_id, **change)
    settings = db.get_settings(chat_id)
    await query.answer("Збережено")
    await query.edit_message_text(format_settings(settings),
                                  reply_markup=menus.settings_keyboard(settings))


@db_guarded
async def cmd_toggle_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔔 button: subscribe or unsubscribe"""
    if get_db().is_subscribed(update.effective_chat.id):
        await cmd_unsubscribe(update, context)
    else:
        await cmd_subscribe(update, context)


# Reply keyboard button action -> handler name (looked up at call time)
PUBLIC_ACTIONS = {
    'status': 'cmd_status',
    'grid': 'cmd_grid',
    'history': 'cmd_history',
    'notify': 'cmd_toggle_notify',
    'settings': 'cmd_settings',
}

PRIVATE_ACTIONS = {
    'status': 'cmd_full_status',
    'chart': 'cmd_chart',
    'stats': 'cmd_stats',
    'subscribers': 'cmd_subscribers',
}


async def on_public_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply keyboard button of the public bot"""
    action = menus.PUBLIC_BUTTONS.get(update.message.text)
    if action:
        await globals()[PUBLIC_ACTIONS[action]](update, context)


async def on_private_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply keyboard button of the private bot"""
    action = menus.PRIVATE_BUTTONS.get(update.message.text)
    if action:
        await globals()[PRIVATE_ACTIONS[action]](update, context)


@db_guarded
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


@db_guarded
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


@db_guarded
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


@db_guarded
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
    ], [
        InlineKeyboardButton("\U0001f4c8 Графік", callback_data="ch:g:timeline:24h"),
    ]])


def _grid_message(period: str) -> str:
    count = GRID_VIEWS[period]
    now = datetime.now(timezone.utc)
    first_start = grid_stats.period_bounds(period, count, now)[0][0]
    intervals = get_db().get_intervals(first_start, now)
    periods = grid_stats.split_by_periods(intervals, period, count, now)
    return format_periods(periods, period)


@db_guarded
async def cmd_grid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /grid command — grid availability statistics"""
    await update.message.reply_text(_grid_message('day'),
                                    reply_markup=_grid_keyboard())


@db_guarded
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
async def cmd_private_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start and /help in the private bot"""
    help_text = (
        "\U0001f510 Бот власника\n\n"
        "/status - Повний статус інвертора\n"
        "/chart - Графіки\n"
        "/stats - Статистика відключень\n"
        "/subscribers - Кількість підписників\n"
        "/ntfytest - Тест сповіщення ntfy\n"
        "/help - Допомога"
    )
    await update.message.reply_text(help_text,
                                    reply_markup=menus.private_keyboard())


def _private_traffic_light(open_iv: Optional[dict], now: datetime) -> str:
    """Traffic light from the DB (the private bot runs no poller)"""
    if not open_iv:
        return format_traffic_light(UNKNOWN, "немає даних", None, now)
    max_gap = 3 * config.POLL_INTERVAL
    if now.timestamp() - open_iv['last_seen_at'].timestamp() > max_gap:
        return format_traffic_light(UNKNOWN, "моніторинг не працює",
                                    open_iv['last_seen_at'], now)
    return format_traffic_light(open_iv['state'], None,
                                open_iv['started_at'], now)


def _battery_line(db, open_iv: Optional[dict], now: datetime) -> Optional[str]:
    """Runtime forecast from DB samples of the current outage (private bot)"""
    if not open_iv or open_iv['state'] != 'off':
        return None
    try:
        rows = db.get_status_history(1)
    except Exception as e:
        logger.warning(f"Failed to load samples for battery forecast: {e}")
        return None
    samples = [(r['timestamp'].timestamp(), r.get('battery_soc')) for r in rows
               if r['timestamp'] >= open_iv['started_at']]
    fc = battery_forecast(samples, now.timestamp(), config.BATTERY_EMPTY_SOC)
    return format_battery_line(fc) if fc else None


@owner_only
async def cmd_full_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status and /fullstatus (private bot)"""
    now = datetime.now(timezone.utc)
    db = get_db()

    open_iv = None
    try:
        open_iv = db.get_open_interval()
    except Exception as e:
        logger.warning(f"Failed to get open interval: {e}")
    header = _private_traffic_light(open_iv, now)
    battery_line = _battery_line(db, open_iv, now)
    if battery_line:
        header += "\n" + battery_line
    schedule_line = _schedule_line(now)
    if schedule_line:
        header += "\n" + schedule_line

    # Fresh data from the RPi without blocking the bot's event loop
    status = await asyncio.to_thread(fetch_status_direct)
    note = ""
    if not status:
        try:
            row = db.get_latest_status()
        except Exception as e:
            logger.warning(f"Failed to get latest sample: {e}")
            row = None
        if row:
            status = status_from_sample(row)
            taken = row['timestamp'].astimezone(KYIV_TZ)
            note = f"\n\n⏱ Дані за {taken:%H:%M %d.%m} (RPi не відповідає)"

    if not status:
        await update.message.reply_text(f"{header}\n\n⚠ Дані інвертора недоступні")
        return

    await update.message.reply_text(
        f"{header}\n\n{format_inverter_details(status)}{note}"
    )


@owner_only
async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /chart command (private bot)"""
    await update.message.reply_text(
        "\U0001f4c8 Оберіть графік:",
        reply_markup=charts.keyboard('p')
    )


async def callback_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Chart buttons in both bots: "ch:<scope>:<type>:<period>" """
    query = update.callback_query
    parsed = charts.parse_callback(query.data)
    if not parsed:
        await query.answer()
        return
    scope, chart_type, period = parsed

    if scope == 'p' and query.from_user.id != config.OWNER_CHAT_ID:
        await query.answer("Доступ заборонено", show_alert=True)
        return

    await query.answer("Генерую графік...")
    try:
        png = await CHART_CACHE.get(chart_type, period)
    except Exception as e:
        logger.error(f"Chart {chart_type}/{period} failed: "
                     f"{type(e).__name__}: {e}")
        await query.message.reply_text("⚠ Не вдалося побудувати графік")
        return

    markup = charts.keyboard(scope, chart_type, period)
    text = charts.caption(chart_type, period)
    if query.message.photo:
        # Period/type switch under an existing chart: replace the picture
        await query.edit_message_media(InputMediaPhoto(png, caption=text),
                                       reply_markup=markup)
    else:
        await query.message.reply_photo(png, caption=text, reply_markup=markup)


@owner_only
@db_guarded
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

    text = format_stats(rows)
    plan_fact = _plan_fact_line(intervals, now, timedelta(days=7))
    if plan_fact:
        text += "\n\n" + plan_fact
    await update.message.reply_text(text)


def _plan_fact_line(intervals, now: datetime, length: timedelta) -> Optional[str]:
    """Planned vs actual outage time, since the schedule has been collected"""
    if not config.DTEK_GROUP:
        return None
    start = now - length
    today = now.astimezone(KYIV_TZ).date()
    try:
        days = get_db().get_schedule(config.DTEK_GROUP,
                                     start.astimezone(KYIV_TZ).date(), today)
    except Exception as e:
        logger.warning(f"Failed to load schedule: {type(e).__name__}")
        return None
    if not days:
        return "\U0001f4c5 План / факт: графік ще не зібрано"
    first = datetime.combine(days[0].day, datetime.min.time(), KYIV_TZ)
    if first <= start:
        label = f"за {length.days} днів"
    else:
        start, label = first, f"з {first:%d.%m}"
    planned = dtek_schedule.planned_seconds(days, start, now)
    fact = grid_stats.window_stats(intervals, start, now).off
    return format_plan_fact(label, planned, fact)


@owner_only
async def cmd_ntfy_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ntfytest (private bot): check the ntfy subscription"""
    ntfy = ntfy_from_config()
    if not ntfy:
        await update.message.reply_text(
            "ntfy не налаштовано (NTFY_TOPIC у .env порожній)")
        return
    notice = Notice("Тест LuxPower", "Сповіщення ntfy працюють",
                    tags=('test_tube',))
    if await asyncio.to_thread(ntfy.send, notice):
        await update.message.reply_text(
            "✅ Надіслано в ntfy — перевірте застосунок")
    else:
        await update.message.reply_text(
            "⚠ Не вдалося надіслати в ntfy (деталі в лозі)")


@owner_only
@db_guarded
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

async def _set_commands(application: Application, commands) -> None:
    """Fill the "/" command menu; a failure must not stop the bot"""
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Failed to set bot commands: {type(e).__name__}")


async def _init_owner_bot(owner_bot: Bot) -> Optional[Bot]:
    """Initialize the private bot used for owner alerts, None if it fails.

    Only the exception type is logged: InvalidToken's message contains the token.
    """
    try:
        await owner_bot.initialize()
        return owner_bot
    except Exception as e:
        logger.error(f"Private bot unavailable for owner alerts: "
                     f"{type(e).__name__}")
        return None


def run_public_bot():
    """Run public bot only"""
    logger.info("Starting public bot...")

    # Initialize database
    get_db()
    poller = get_poller()
    private_bot = Bot(config.PRIVATE_BOT_TOKEN)

    async def on_start(application: Application):
        """Wire alerts and start polling once the bot loop is running"""
        await _set_commands(application, menus.PUBLIC_COMMANDS)
        ntfy = ntfy_from_config()
        alert_manager = AlertManager(application.bot,
                                     await _init_owner_bot(private_bot),
                                     owner_channels=[ntfy] if ntfy else [])
        alert_manager.set_event_loop(asyncio.get_running_loop())
        poller.add_state_callback(alert_manager.on_grid_change)
        poller.add_unknown_callback(alert_manager.on_unknown_change)
        poller.add_battery_callback(alert_manager.on_low_battery)
        if poller.schedule is not None:
            poller.schedule.add_reminder_callback(alert_manager.on_planned_outage)
            poller.schedule.add_group_callback(alert_manager.on_group_change)
        poller.start()

    async def on_stop(application: Application):
        """Stop polling while the bot's HTTP client is still open"""
        poller.stop()

    async def on_shutdown(application: Application):
        try:
            await private_bot.shutdown()
        except Exception as e:
            logger.warning(f"Private bot shutdown: {type(e).__name__}")

    app = (Application.builder()
           .token(config.PUBLIC_BOT_TOKEN)
           .post_init(on_start)
           .post_stop(on_stop)
           .post_shutdown(on_shutdown)
           .build())

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("grid", cmd_grid))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(callback_settings, pattern="^set:"))

    app.add_handler(CallbackQueryHandler(callback_history_detail, pattern="^history_"))
    app.add_handler(CallbackQueryHandler(callback_grid, pattern="^grid_"))
    app.add_handler(CallbackQueryHandler(callback_chart, pattern="^ch:"))
    app.add_handler(MessageHandler(filters.Text(list(menus.PUBLIC_BUTTONS)),
                                   on_public_button))

    logger.info("Public bot started")

    # Run
    app.run_polling(allowed_updates=Update.ALL_TYPES)

    close_db()


def run_private_bot():
    """Run private bot only"""
    logger.info("Starting private bot...")

    async def on_start(application: Application):
        await _set_commands(application, menus.PRIVATE_COMMANDS)

    # Create application
    app = (Application.builder()
           .token(config.PRIVATE_BOT_TOKEN)
           .post_init(on_start)
           .build())

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_private_help))
    app.add_handler(CommandHandler("help", cmd_private_help))
    app.add_handler(CommandHandler("status", cmd_full_status))
    app.add_handler(CommandHandler("fullstatus", cmd_full_status))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("subscribers", cmd_subscribers))
    app.add_handler(CommandHandler("ntfytest", cmd_ntfy_test))

    app.add_handler(CallbackQueryHandler(callback_chart, pattern="^ch:"))
    app.add_handler(MessageHandler(filters.Text(list(menus.PRIVATE_BUTTONS)),
                                   on_private_button))

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
