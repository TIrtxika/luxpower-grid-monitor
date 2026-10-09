"""
Alert system for grid state changes
Sends notifications to subscribers and owner
"""

import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict
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

    def __init__(self, public_bot: Bot, private_bot: Bot = None):
        self.public_bot = public_bot
        self.private_bot = private_bot
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Set event loop for async operations"""
        self._loop = loop

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
        msg += f"\U0001f321 Температура:\n"
        msg += f"   Інвертор: {temp.get('inverter', 0)}°C\n"
        msg += f"   Радіатор: {temp.get('radiator', 0)}°C\n"

        age = status.get('data_age_seconds', 0)
        if age:
            msg += f"\n\u23f1 Дані оновлено {age} сек тому"

        return msg

    async def _send_message_async(self, bot: Bot, chat_id: int,
                                  text: str) -> str:
        """Send message; returns SEND_OK / SEND_BLOCKED / SEND_ERROR"""
        try:
            await bot.send_message(chat_id=chat_id, text=text)
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
            subscribers = db.get_active_subscribers()
        except Exception as e:
            logger.error(f"Failed to load subscribers: {e}")
            subscribers = []

        logger.info(f"Sending grid alert to {len(subscribers)} subscribers")

        for chat_id in subscribers:
            result = await self._send_message_async(self.public_bot, chat_id,
                                                     message)
            if result == SEND_BLOCKED:
                try:
                    db.remove_subscriber(chat_id)
                    logger.info(f"Deactivated subscriber {chat_id}")
                except Exception as e:
                    logger.error(f"Failed to deactivate {chat_id}: {e}")

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

    def on_grid_change(self, change: GridChange, status: Optional[Dict]):
        """Poller callback: grid ON <-> OFF"""
        self._run_async(self.send_grid_alert(change, status))

    def on_unknown_change(self, active: bool, reason: Optional[str],
                          seconds: int):
        """Poller callback: unknown state alert / data restored / downtime"""
        self._run_async(self._send_unknown_alert(active, reason, seconds))

    async def _send_unknown_alert(self, active: bool, reason: Optional[str],
                                  seconds: int):
        """Unknown-state alerts go to the owner only"""
        if not config.OWNER_CHAT_ID:
            return
        bot = self.private_bot or self.public_bot
        now = kyiv_now().strftime('%H:%M:%S %d.%m.%Y')
        duration = format_seconds(seconds)

        if active:
            reason_text = UNKNOWN_REASONS_UA.get(reason, reason or "невідомо")
            msg = (f"\U0001f7e1 Немає даних про мережу\n"
                   f"Причина: {reason_text}\n"
                   f"Вже {duration}\n\n{now}")
        elif reason == REASON_MONITOR_DOWNTIME:
            msg = (f"⚠️ Моніторинг не працював {duration}\n"
                   f"Стан мережі за цей час невідомий\n\n{now}")
        else:
            msg = (f"✅ Дані знову надходять\n"
                   f"Не було даних: {duration}\n\n{now}")

        await self._send_message_async(bot, config.OWNER_CHAT_ID, msg)

    async def send_status_to_owner(self, status: Dict):
        """Send status to owner (private bot)"""
        if not self.private_bot or not config.OWNER_CHAT_ID:
            return

        message = self._format_private_status(status)
        await self._send_message_async(
            self.private_bot, config.OWNER_CHAT_ID, message
        )
