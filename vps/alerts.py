"""
Alert system for grid state changes
Sends notifications to subscribers and owner
"""

import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict, Sequence
from zoneinfo import ZoneInfo

# Kyiv timezone (EET/EEST, follows DST)
KYIV_TZ = ZoneInfo("Europe/Kyiv")


def kyiv_now() -> datetime:
    """Get current time in Kyiv timezone"""
    return datetime.now(KYIV_TZ)

from telegram import Bot
from telegram.error import Forbidden, TelegramError

import config
from database import get_db
from schedule import PlannedOutage
from subscriptions import Settings, in_quiet_hours, wants
from notifiers import (
    Notice, PRIORITY_DEFAULT, PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_URGENT,
)
from grid_state import (
    GridChange, ON,
    REASON_RPI_UNREACHABLE, REASON_DONGLE_OFFLINE, REASON_STALE_DATA,
    REASON_NO_GRID_DATA, REASON_MONITOR_DOWNTIME,
)

logger = logging.getLogger(__name__)

SEND_OK = 'ok'
SEND_BLOCKED = 'blocked'
SEND_ERROR = 'error'

UNKNOWN_REASONS_UA = {
    REASON_RPI_UNREACHABLE: "RPi недоступний",
    REASON_DONGLE_OFFLINE: "інвертор не відповідає (WiFi-донгл офлайн)",
    REASON_STALE_DATA: "дані застарілі",
    REASON_NO_GRID_DATA: "інвертор не повідомляє стан мережі",
    REASON_MONITOR_DOWNTIME: "моніторинг не працював",
}


def _log_future_error(future):
    """Done-callback for alerts sent from the poller thread"""
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.error(f"Background alert failed: {type(exc).__name__}: {exc}")


def format_seconds(seconds: int) -> str:
    """'2 год 5 хв' / '5 хв 3 сек' / '40 сек'"""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} год {minutes} хв" if minutes else f"{hours} год"
    if minutes:
        return f"{minutes} хв {secs} сек" if secs else f"{minutes} хв"
    return f"{secs} сек"


class AlertManager:
    """Manages alerts and notifications"""

    def __init__(self, public_bot: Bot, private_bot: Bot = None,
                 owner_channels: Sequence = ()):
        self.public_bot = public_bot
        self.private_bot = private_bot
        # Extra owner channels outside Telegram (objects with send(Notice))
        self.owner_channels = list(owner_channels)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Set event loop for async operations"""
        self._loop = loop

    async def _notify_owner_channels(self, notice: Notice):
        """Send to ntfy & co. in a thread; failures never affect Telegram"""
        for channel in self.owner_channels:
            try:
                await asyncio.to_thread(channel.send, notice)
            except Exception as e:
                logger.error(f"Owner channel failed: {type(e).__name__}")

    def _format_grid_message(self, grid_on: bool, status: Dict,
                             duration: int = None,
                             approximate: bool = False) -> str:
        """Format grid state change message"""
        if grid_on:
            voltage = (status.get('grid') or {}).get('voltage', 0)
            msg = f"⚡ Електромережу УВІМКНЕНО\nНапруга: {voltage}V"
            if duration:
                msg += f"\nВідключення тривало: {format_seconds(duration)}"
        else:
            msg = "❌ Електромережу ВИМКНЕНО"

        if approximate:
            msg += "\n(зміна сталася, поки не було даних — час приблизний)"

        msg += f"\n\n{kyiv_now().strftime('%H:%M:%S %d.%m.%Y')}"
        return msg

    def _format_private_status(self, status: Dict) -> str:
        """Format detailed status for private bot"""
        grid = status.get('grid', {})
        battery = status.get('battery', {})
        output = status.get('output', {})
        temp = status.get('temperature', {})

        msg = "\U0001f50b Статус інвертора\n\n"

        # Grid
        grid_emoji = "\u2705" if grid.get('available') else "\u274c"
        msg += f"{grid_emoji} Мережа: {grid.get('voltage', 0)}V / "
        msg += f"{grid.get('frequency', 0)}Hz\n\n"

        # Battery
        soc = battery.get('soc', 0)
        if soc >= 80:
            bat_emoji = "\U0001f50b"
        elif soc >= 40:
            bat_emoji = "\U0001faab"
        else:
            bat_emoji = "\U0001f6a8"

        msg += f"{bat_emoji} Батарея: {soc}%\n"
        msg += f"   Напруга: {battery.get('voltage', 0)}V\n"
        msg += f"   Струм: {battery.get('current', 0)}A\n"
        msg += f"   Потужність: {battery.get('power', 0)}W\n\n"

        # Load
        msg += f"\U0001f3e0 Навантаження: {output.get('load_power', 0)}W\n"
        msg += f"   Вихід: {output.get('voltage', 0)}V / "
        msg += f"{output.get('frequency', 0)}Hz\n\n"

        # Temperature
        msg += "\U0001f321 Температура:\n"
        msg += f"   Інвертор: {temp.get('inverter', 0)}°C\n"
        msg += f"   Радіатор: {temp.get('radiator', 0)}°C\n"

        age = status.get('data_age_seconds', 0)
        if age:
            msg += f"\n\u23f1 Дані оновлено {age} сек тому"

        return msg

    async def _send_message_async(self, bot: Bot, chat_id: int,
                                  text: str, silent: bool = False) -> str:
        """Send message; returns SEND_OK / SEND_BLOCKED / SEND_ERROR"""
        try:
            await bot.send_message(chat_id=chat_id, text=text,
                                   disable_notification=silent)
            return SEND_OK
        except Forbidden as e:
            logger.warning(f"Chat {chat_id} blocked the bot: {e}")
            return SEND_BLOCKED
        except TelegramError as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return SEND_ERROR

    def _run_async(self, coro):
        """Run coroutine on the bot's event loop (called from poller thread).

        run_polling drives the loop with separate run_until_complete calls,
        so is_running() can be False in between: queue the coroutine anyway,
        it runs on the next turn. Never run the bot's client on another loop.
        """
        if self._loop is None:
            logger.warning("Bot event loop not set, sending synchronously")
            asyncio.run(coro)
            return
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(_log_future_error)

    async def _deliver(self, db, subscribers: Sequence[Settings], message: str):
        """Public bot fan-out; drops subscribers who blocked the bot"""
        now = kyiv_now()
        for settings in subscribers:
            # Quiet hours: deliver without sound, never drop the message
            result = await self._send_message_async(
                self.public_bot, settings.chat_id, message,
                silent=in_quiet_hours(settings, now))
            if result == SEND_BLOCKED:
                try:
                    db.remove_subscriber(settings.chat_id)
                    logger.info(f"Deactivated subscriber {settings.chat_id}")
                except Exception as e:
                    logger.error(f"Failed to deactivate {settings.chat_id}: {e}")

    async def send_grid_alert(self, change: GridChange, status: Optional[Dict]):
        """Save the event, then notify subscribers, channel and owner"""
        status = status or {}
        grid_on = change.state == ON
        db = get_db()

        try:
            db.save_event(
                'grid_on' if grid_on else 'grid_off',
                {
                    'voltage': (status.get('grid') or {}).get('voltage'),
                    'duration_seconds': change.duration_s,
                    'approximate': change.approximate,
                }
            )
        except Exception as e:
            logger.error(f"Failed to save grid event: {e}")

        message = self._format_grid_message(grid_on, status, change.duration_s,
                                            change.approximate)

        try:
            subscribers = db.get_subscriber_settings()
        except Exception as e:
            logger.error(f"Failed to load subscribers: {e}")
            subscribers = []

        logger.info(f"Sending grid alert to {len(subscribers)} subscribers")

        await self._deliver(db, [s for s in subscribers if wants(s, grid_on)],
                            message)

        # Send to public channel if configured
        if config.PUBLIC_CHANNEL_ID:
            try:
                channel_id = int(config.PUBLIC_CHANNEL_ID)
                await self._send_message_async(self.public_bot, channel_id, message)
            except ValueError:
                pass

        # Detailed status to owner via private bot
        if self.private_bot and config.OWNER_CHAT_ID and status:
            await self._send_message_async(
                self.private_bot, config.OWNER_CHAT_ID,
                self._format_private_status(status)
            )

        # Owner channels outside Telegram: same text, plain title
        body = message.split("\n", 1)[1].strip() if "\n" in message else message
        if grid_on:
            notice = Notice("Світло є", body, PRIORITY_DEFAULT, ('green_circle',))
        else:
            notice = Notice("Світла немає", body, PRIORITY_HIGH, ('red_circle',))
        await self._notify_owner_channels(notice)

    def on_grid_change(self, change: GridChange, status: Optional[Dict]):
        """Poller callback: grid ON <-> OFF"""
        self._run_async(self.send_grid_alert(change, status))

    def on_unknown_change(self, active: bool, reason: Optional[str],
                          seconds: int):
        """Poller callback: unknown state alert / data restored / downtime"""
        self._run_async(self._send_unknown_alert(active, reason, seconds))

    async def _send_unknown_alert(self, active: bool, reason: Optional[str],
                                  seconds: int):
        """Unknown-state alerts go to the owner only (Telegram + channels)"""
        now = kyiv_now().strftime('%H:%M:%S %d.%m.%Y')
        duration = format_seconds(seconds)

        if active:
            reason_text = UNKNOWN_REASONS_UA.get(reason, reason or "невідомо")
            emoji, title = "\U0001f7e1", "Немає даних про мережу"
            body = f"Причина: {reason_text}\nВже {duration}\n\n{now}"
            priority, tags = PRIORITY_DEFAULT, ('yellow_circle',)
        elif reason == REASON_MONITOR_DOWNTIME:
            emoji, title = "⚠️", f"Моніторинг не працював {duration}"
            body = f"Стан мережі за цей час невідомий\n\n{now}"
            priority, tags = PRIORITY_DEFAULT, ('warning',)
        else:
            emoji, title = "✅", "Дані знову надходять"
            body = f"Не було даних: {duration}\n\n{now}"
            priority, tags = PRIORITY_LOW, ('white_check_mark',)

        if config.OWNER_CHAT_ID:
            bot = self.private_bot or self.public_bot
            await self._send_message_async(bot, config.OWNER_CHAT_ID,
                                           f"{emoji} {title}\n{body}")
        await self._notify_owner_channels(Notice(title, body, priority, tags))
    def on_low_battery(self, level: int, soc: int, forecast):
        """Poller callback: SOC crossed a threshold during an outage"""
        self._run_async(self._send_low_battery(level, soc, forecast))

    async def _send_low_battery(self, level: int, soc: int, forecast):
        """Low battery goes to the owner only (Telegram + channels)"""
        now = kyiv_now().strftime('%H:%M:%S %d.%m.%Y')
        title = f"Батарея {soc}%"
        if forecast is not None:
            estimate = (f"Вистачить ще ~{format_seconds(int(forecast.seconds_left))} "
                        f"(−{forecast.rate_per_hour:.0f}%/год)")
        else:
            estimate = "Світла немає, прогноз ще недоступний"
        body = f"{estimate}\n\n{now}"

        if level <= min(config.BATTERY_ALERT_LEVELS):
            emoji, priority, tags = "\U0001f6a8", PRIORITY_URGENT, ('rotating_light',)
        else:
            emoji, priority, tags = "\U0001faab", PRIORITY_HIGH, ('battery',)

        if config.OWNER_CHAT_ID:
            bot = self.private_bot or self.public_bot
            await self._send_message_async(bot, config.OWNER_CHAT_ID,
                                           f"{emoji} {title}\n{body}")
        await self._notify_owner_channels(Notice(title, body, priority, tags))

    def on_planned_outage(self, outage: PlannedOutage):
        """Schedule callback: a planned outage starts soon"""
        self._run_async(self._send_reminder(outage))

    def on_group_change(self, configured: str, actual: str):
        """Schedule callback: the address moved to another group"""
        self._run_async(self._send_group_change(configured, actual))

    async def _send_reminder(self, outage: PlannedOutage):
        """Reminder to subscribers who want it, and to the owner"""
        start = outage.start.astimezone(KYIV_TZ).strftime('%H:%M')
        end = outage.end.astimezone(KYIV_TZ).strftime('%H:%M')
        title = f"Відключення за графіком о {start}"
        body = f"За графіком ДТЕК світла не буде {start}–{end}"
        message = f"\u23f0 {title}\n{body}"

        db = get_db()
        try:
            subscribers = db.get_subscriber_settings()
        except Exception as e:
            logger.error(f"Failed to load subscribers: {e}")
            subscribers = []
        owner = config.OWNER_CHAT_ID
        # The owner gets it once, from the private bot when there is one
        skip_owner = bool(owner and self.private_bot)
        await self._deliver(db, [s for s in subscribers if s.remind_enabled
                                 and not (skip_owner and s.chat_id == owner)],
                            message)

        # The owner's own /settings (public bot) apply; defaults if not subscribed
        mine = next((s for s in subscribers if s.chat_id == owner),
                    Settings(owner))
        if not mine.remind_enabled:
            return
        quiet = in_quiet_hours(mine, kyiv_now())
        if skip_owner:
            await self._send_message_async(self.private_bot, owner, message,
                                           silent=quiet)
        await self._notify_owner_channels(
            Notice(title, body, PRIORITY_LOW if quiet else PRIORITY_DEFAULT,
                   ('calendar',)))

    async def _send_group_change(self, configured: str, actual: str):
        """The address now belongs to another schedule group: owner only"""
        title = f"Змінилась група графіка: {configured} → {actual}"
        body = (f"YASNO повертає для вашої адреси групу {actual}. "
                f"Онови DTEK_GROUP={actual} у .env і перезапусти ботів.")
        if config.OWNER_CHAT_ID:
            bot = self.private_bot or self.public_bot
            await self._send_message_async(bot, config.OWNER_CHAT_ID,
                                           f"\u26a0\ufe0f {title}\n{body}")
        await self._notify_owner_channels(
            Notice(title, body, PRIORITY_HIGH, ('warning',)))

    async def send_status_to_owner(self, status: Dict):
        """Send status to owner (private bot)"""
        if not self.private_bot or not config.OWNER_CHAT_ID:
            return

        message = self._format_private_status(status)
        await self._send_message_async(
            self.private_bot, config.OWNER_CHAT_ID, message
        )
