"""
Battery forecast in the private /status and low-battery alert wiring.
Run: python -m unittest test_bot_battery
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from battery import Forecast
from messages import format_battery_line


class BatteryLineTest(unittest.TestCase):
    def test_line(self):
        self.assertEqual(format_battery_line(Forecast(29, 18.0, 6000)),
                         "\U0001faab Батареї вистачить ще ~1 год 40 хв (−18%/год)")


class PrivateStatusForecastTest(unittest.TestCase):
    def full_status(self, state):
        now = datetime.now(timezone.utc)
        db = MagicMock()
        db.get_open_interval.return_value = {
            'state': state, 'started_at': now - timedelta(minutes=40),
            'last_seen_at': now - timedelta(seconds=30)}
        # 5-minute samples, 1 % per 5 min = 12 %/h
        db.get_status_history.return_value = [
            {'timestamp': now - timedelta(minutes=5 * i), 'battery_soc': 50 - (6 - i)}
            for i in range(6, -1, -1)]
        db.get_latest_status.return_value = None
        update = MagicMock()
        update.effective_chat.id = bot.config.OWNER_CHAT_ID
        update.message.reply_text = AsyncMock()
        with patch.object(bot, 'get_db', return_value=db), \
             patch.object(bot, 'fetch_status_direct', return_value=None):
            asyncio.run(bot.cmd_full_status(update, MagicMock()))
        return update.message.reply_text.await_args.args[0]

    def test_forecast_shown_during_outage(self):
        text = self.full_status('off')
        self.assertIn("Батареї вистачить ще ~", text)
        self.assertIn("12%/год", text)

    def test_no_forecast_when_grid_is_on(self):
        self.assertNotIn("Батареї вистачить", self.full_status('on'))


class WiringTest(unittest.TestCase):
    def test_public_bot_wires_low_battery_alerts(self):
        builder = MagicMock()
        for name in ('token', 'post_init', 'post_stop', 'post_shutdown'):
            getattr(builder, name).return_value = builder
        poller = MagicMock()
        with patch.object(bot.Application, 'builder', return_value=builder), \
             patch.object(bot, 'get_db'), patch.object(bot, 'close_db'), \
             patch.object(bot, 'get_poller', return_value=poller), \
             patch.object(bot, 'Bot'), \
             patch.object(bot, '_init_owner_bot', new=AsyncMock(return_value=None)), \
             patch.object(bot, 'AlertManager') as manager:
            bot.run_public_bot()
            application = MagicMock()
            application.bot.set_my_commands = AsyncMock()
            asyncio.run(builder.post_init.call_args.args[0](application))
        poller.add_battery_callback.assert_called_once_with(
            manager.return_value.on_low_battery)


if __name__ == '__main__':
    unittest.main()
