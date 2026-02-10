"""
Alert system for grid state changes
Sends notifications to subscribers and owner
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

# Kyiv timezone (UTC+2)
KYIV_TZ = timezone(timedelta(hours=2))


def kyiv_now() -> datetime:
    """Get current time in Kyiv timezone"""
    return datetime.now(KYIV_TZ)

from telegram import Bot
from telegram.error import TelegramError

import config
from database import get_db

logger = logging.getLogger(__name__)


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
                             duration: int = None) -> str:
        """Format grid state change message"""
        if grid_on:
            emoji = "\u26a1"  # Lightning
            state = "УВІМКНЕНО"
            voltage = status.get('grid', {}).get('voltage', 0)
            msg = f"{emoji} Електромережу {state}\n"
            msg += f"Напруга: {voltage}V"
            if duration:
                minutes = duration // 60
                seconds = duration % 60
                if minutes > 0:
                    msg += f"\nВідключення тривало: {minutes} хв {seconds} сек"
                else:
                    msg += f"\nВідключення тривало: {seconds} сек"
        else:
            emoji = "\u274c"  # Red X
            state = "ВИМКНЕНО"
            msg = f"{emoji} Електромережу {state}"

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
                                  text: str) -> bool:
        """Send message asynchronously"""
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except TelegramError as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return False

    def _run_async(self, coro):
        """Run coroutine in event loop"""
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        if self._loop.is_running():
            asyncio.ensure_future(coro, loop=self._loop)
        else:
            self._loop.run_until_complete(coro)

    async def send_grid_alert(self, grid_on: bool, status: Dict,
                              duration: int = None):
        """Send grid state change alert to all subscribers"""
        message = self._format_grid_message(grid_on, status, duration)

        # Get subscribers
        db = get_db()
        subscribers = db.get_active_subscribers()

        logger.info(f"Sending grid alert to {len(subscribers)} subscribers")

        # Send to all subscribers
        for chat_id in subscribers:
            await self._send_message_async(self.public_bot, chat_id, message)

        # Send to public channel if configured
        if config.PUBLIC_CHANNEL_ID:
            try:
                channel_id = int(config.PUBLIC_CHANNEL_ID)
                await self._send_message_async(self.public_bot, channel_id, message)
            except ValueError:
                pass

        # Also send detailed status to owner via private bot
        if self.private_bot and config.OWNER_CHAT_ID:
            private_msg = self._format_private_status(status)
            await self._send_message_async(
                self.private_bot, config.OWNER_CHAT_ID, private_msg
            )

        # Log event to database
        db.save_event(
            'grid_on' if grid_on else 'grid_off',
            {
                'voltage': status.get('grid', {}).get('voltage'),
                'duration_seconds': duration
            }
        )

    def on_grid_state_change(self, old_state: bool, new_state: bool,
                             status: Dict):
        """Callback for grid state changes from poller"""
        # Calculate duration if grid came back
        duration = None
        if new_state:  # Grid ON
            db = get_db()
            events = db.get_events('grid_off', hours=24, limit=1)
            if events:
                last_off = events[0]
                off_time = last_off['timestamp'].timestamp()
                duration = int(status.get('timestamp', 0) - off_time)

        # Send alert
        self._run_async(self.send_grid_alert(new_state, status, duration))

    def on_rpi_state_change(self, available: bool, detail: int):
        """Callback for RPi availability changes from poller.

        Args:
            available: True if RPi recovered, False if became unreachable
            detail: consecutive failures count (when unavailable) or
                    downtime in seconds (when recovered)
        """
        self._run_async(self._send_rpi_alert(available, detail))

    async def _send_rpi_alert(self, available: bool, detail: int):
        """Send RPi availability alert to owner"""
        bot = self.private_bot or self.public_bot
        if not config.OWNER_CHAT_ID:
            return

        now = kyiv_now().strftime('%H:%M:%S %d.%m.%Y')

        if available:
            minutes = detail // 60
            seconds = detail % 60
            if minutes > 0:
                duration_str = f"{minutes} хв {seconds} сек"
            else:
                duration_str = f"{seconds} сек"
            msg = (f"\u2705 RPi знову доступний\n"
                   f"Був недоступний: {duration_str}\n\n{now}")
        else:
            msg = (f"\u26a0\ufe0f RPi недоступний!\n"
                   f"Помилок підряд: {detail}\n"
                   f"Дані можуть бути застарілими\n\n{now}")

        await self._send_message_async(bot, config.OWNER_CHAT_ID, msg)

    async def send_status_to_owner(self, status: Dict):
        """Send status to owner (private bot)"""
        if not self.private_bot or not config.OWNER_CHAT_ID:
            return

        message = self._format_private_status(status)
        await self._send_message_async(
            self.private_bot, config.OWNER_CHAT_ID, message
        )
