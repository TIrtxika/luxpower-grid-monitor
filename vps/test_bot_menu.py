"""
Stage 2: command menus, reply keyboard buttons, traffic-light status.
Run: python -m unittest test_bot_menu
"""

import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import ReplyKeyboardMarkup

import bot
import menus

SAMPLE = {
    'timestamp': datetime(2026, 10, 9, 9, 5, tzinfo=timezone.utc),
    'grid_available': True, 'grid_voltage': 229.5, 'grid_frequency': 50.0,
    'battery_soc': 77, 'battery_voltage': 52.0, 'battery_current': 1.5,
    'battery_power': 78.0, 'load_power': 300, 'output_voltage': 230.0,
    'output_frequency': 50.0, 'inverter_temp': 41, 'radiator_temp': 36.0,
    'dc_bus_voltage': 380.0,
}


def text_update(text=None):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = bot.config.OWNER_CHAT_ID
    update.effective_user.username = "owner"
    update.effective_user.first_name = "Owner"
    return update


def replied(update):
    return update.message.reply_text.await_args


def run(coro):
    return asyncio.run(coro)


class ButtonRoutingTest(unittest.TestCase):
    def test_public_status_button_runs_status(self):
        update = text_update(menus.BTN_STATUS)
        with patch.object(bot, 'cmd_status', new=AsyncMock()) as handler:
            run(bot.on_public_button(update, MagicMock()))
        handler.assert_awaited_once()

    def test_public_stats_button_runs_grid(self):
        update = text_update(menus.BTN_STATS)
        with patch.object(bot, 'cmd_grid', new=AsyncMock()) as handler:
            run(bot.on_public_button(update, MagicMock()))
        handler.assert_awaited_once()

    def test_private_charts_button_runs_chart(self):
        update = text_update(menus.BTN_CHARTS)
        with patch.object(bot, 'cmd_chart', new=AsyncMock()) as handler:
            run(bot.on_private_button(update, MagicMock()))
        handler.assert_awaited_once()


class NotifyToggleTest(unittest.TestCase):
    def toggle(self, subscribed):
        db = MagicMock()
        db.is_subscribed.return_value = subscribed
        update = text_update(menus.BTN_NOTIFY)
        with patch.object(bot, 'get_db', return_value=db):
            run(bot.cmd_toggle_notify(update, MagicMock()))
        return db, replied(update).args[0]

    def test_subscribes_when_not_subscribed(self):
        db, text = self.toggle(False)
        db.add_subscriber.assert_called_once()
        self.assertIn("підписались", text)

    def test_unsubscribes_when_subscribed(self):
        db, text = self.toggle(True)
        db.remove_subscriber.assert_called_once()
        self.assertIn("відписались", text)


class KeyboardShownTest(unittest.TestCase):
    def test_start_shows_public_keyboard(self):
        update = text_update()
        run(bot.cmd_start(update, MagicMock()))
        markup = replied(update).kwargs['reply_markup']
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        self.assertEqual(markup.keyboard[0][0].text, menus.BTN_STATUS)

    def test_help_shows_public_keyboard(self):
        update = text_update()
        run(bot.cmd_help(update, MagicMock()))
        self.assertIsInstance(replied(update).kwargs['reply_markup'],
                              ReplyKeyboardMarkup)

    def test_private_help_shows_private_keyboard(self):
        update = text_update()
        run(bot.cmd_private_help(update, MagicMock()))
        call = replied(update)
        self.assertIn("/chart", call.args[0])
        self.assertEqual(call.kwargs['reply_markup'].keyboard[0][1].text,
                         menus.BTN_CHARTS)


class PublicStatusTest(unittest.TestCase):
    def status(self, state, reason=None, data_age=5):
        now = datetime.now(timezone.utc)
        poller = MagicMock()
        poller.get_effective_state.return_value = state
        poller.get_state_reason.return_value = reason
        poller.get_last_status.return_value = {
            'timestamp': time.time() - data_age,
            'grid': {'available': state == 'on', 'voltage': 231},
        }
        db = MagicMock()
        db.get_open_interval.return_value = {
            'state': state, 'started_at': now - timedelta(minutes=30),
            'last_seen_at': now}
        update = text_update()
        with patch.object(bot, 'get_poller', return_value=poller), \
             patch.object(bot, 'get_db', return_value=db):
            run(bot.cmd_status(update, MagicMock()))
        return replied(update).args[0]

    def test_green(self):
        text = self.status('on')
        self.assertTrue(text.startswith("\U0001f7e2 Світло є\nЗ "))
        self.assertIn("Напруга: 231V", text)

    def test_red(self):
        self.assertTrue(self.status('off').startswith("\U0001f534 Світла немає"))

    def test_yellow_with_reason_and_age(self):
        text = self.status('unknown', 'stale_data', data_age=600)
        self.assertTrue(text.startswith("\U0001f7e1 Невідомо — дані застарілі"))
        self.assertIn("Останні дані: 10 хв тому", text)


class PrivateStatusTest(unittest.TestCase):
    def full_status(self, fresh, last_seen_ago=timedelta(seconds=30)):
        now = datetime.now(timezone.utc)
        db = MagicMock()
        db.get_open_interval.return_value = {
            'state': 'on', 'started_at': now - timedelta(hours=2),
            'last_seen_at': now - last_seen_ago}
        db.get_latest_status.return_value = SAMPLE
        update = text_update()
        with patch.object(bot, 'get_db', return_value=db), \
             patch.object(bot, 'fetch_status_direct', return_value=fresh):
            run(bot.cmd_full_status(update, MagicMock()))
        return replied(update).args[0]

    def test_fresh_rpi_data(self):
        fresh = {'grid': {'voltage': 231, 'frequency': 50},
                 'battery': {'soc': 90}, 'output': {}, 'temperature': {}}
        text = self.full_status(fresh)
        self.assertTrue(text.startswith("\U0001f7e2 Світло є"))
        self.assertIn("Батарея: 90%", text)
        self.assertNotIn("Дані за", text)

    def test_falls_back_to_latest_db_sample(self):
        text = self.full_status(None)
        self.assertIn("Батарея: 77%", text)
        self.assertIn("Дані за 12:05 09.10", text)

    def test_stale_interval_means_monitor_down(self):
        text = self.full_status(None, last_seen_ago=timedelta(hours=1))
        self.assertTrue(text.startswith("\U0001f7e1 Невідомо — моніторинг не працює"))


class CommandMenuTest(unittest.TestCase):
    def builder(self):
        builder = MagicMock()
        for name in ('token', 'post_init', 'post_stop', 'post_shutdown'):
            getattr(builder, name).return_value = builder
        builder.build.return_value = MagicMock()
        return builder

    def test_public_bot_sets_commands_on_start(self):
        builder = self.builder()
        with patch.object(bot.Application, 'builder', return_value=builder), \
             patch.object(bot, 'get_db'), patch.object(bot, 'close_db'), \
             patch.object(bot, 'get_poller'), patch.object(bot, 'Bot'), \
             patch.object(bot, '_init_owner_bot', new=AsyncMock(return_value=None)):
            bot.run_public_bot()
            application = MagicMock()
            application.bot.set_my_commands = AsyncMock()
            run(builder.post_init.call_args.args[0](application))
        application.bot.set_my_commands.assert_awaited_once_with(menus.PUBLIC_COMMANDS)

    def test_private_bot_sets_commands_and_registers_help(self):
        builder = self.builder()
        app = builder.build.return_value
        with patch.object(bot.Application, 'builder', return_value=builder):
            bot.run_private_bot()
        application = MagicMock()
        application.bot.set_my_commands = AsyncMock()
        run(builder.post_init.call_args.args[0](application))
        application.bot.set_my_commands.assert_awaited_once_with(menus.PRIVATE_COMMANDS)

        commands = set()
        for call in app.add_handler.call_args_list:
            commands |= set(getattr(call.args[0], 'commands', ()))
        self.assertTrue({'start', 'help', 'status', 'chart'} <= commands)


if __name__ == '__main__':
    unittest.main()
