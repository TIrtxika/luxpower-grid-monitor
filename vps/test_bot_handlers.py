"""
Bot handlers on interval statistics (empty DB, grid views, logging).
Run: python -m unittest test_bot_handlers
"""

import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from messages import BAR_UNKNOWN
from stats import Interval


class FakeDb:
    def __init__(self, intervals=()):
        self.intervals = list(intervals)

    def get_intervals(self, start, end):
        return [iv for iv in self.intervals if iv.start < end and iv.end > start]

    def get_open_interval(self):
        return None


def message_update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = bot.config.OWNER_CHAT_ID
    return update


class BotHandlersTest(unittest.TestCase):
    def run_cmd(self, handler, db):
        update = message_update()
        with patch.object(bot, 'get_db', return_value=db):
            asyncio.run(handler(update, MagicMock()))
        return update.message.reply_text.await_args.args[0]

    def test_httpx_does_not_log_requests(self):
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_history_on_empty_db(self):
        self.assertIn("відключень не було", self.run_cmd(bot.cmd_history, FakeDb()))

    def test_grid_on_empty_db_is_all_unknown(self):
        text = self.run_cmd(bot.cmd_grid, FakeDb())
        self.assertEqual(text.count(BAR_UNKNOWN * 16), 7)

    def test_year_view_has_twelve_months(self):
        with patch.object(bot, 'get_db', return_value=FakeDb()):
            text = bot._grid_message('month')
        self.assertIn("за рік", text)
        self.assertEqual(text.count(BAR_UNKNOWN * 16), 12)

    def test_stats_reports_availability(self):
        now = datetime.now(timezone.utc)
        db = FakeDb([Interval('on', now - timedelta(days=2), now - timedelta(hours=2)),
                     Interval('off', now - timedelta(hours=2), now, ongoing=True)])
        text = self.run_cmd(bot.cmd_stats, db)
        self.assertIn("За 24 години", text)
        self.assertIn("Відключень: 1", text)
        self.assertIn("Доступність", text)


class OwnerBotInitTest(unittest.TestCase):
    def test_init_failure_does_not_log_token(self):
        from telegram.error import InvalidToken
        owner = MagicMock()
        owner.initialize = AsyncMock(
            side_effect=InvalidToken("The token `123:SECRET` was rejected"))
        with self.assertLogs('bot', level='ERROR') as logs:
            result = asyncio.run(bot._init_owner_bot(owner))
        self.assertIsNone(result)
        self.assertNotIn("SECRET", "\n".join(logs.output))

    def test_init_success_returns_bot(self):
        owner = MagicMock()
        owner.initialize = AsyncMock()
        self.assertIs(asyncio.run(bot._init_owner_bot(owner)), owner)


class PublicBotWiringTest(unittest.TestCase):
    def test_poller_stops_in_post_stop_before_http_shutdown(self):
        builder = MagicMock()
        for name in ('token', 'post_init', 'post_stop', 'post_shutdown'):
            getattr(builder, name).return_value = builder
        app = MagicMock()
        builder.build.return_value = app
        poller = MagicMock()

        with patch.object(bot.Application, 'builder', return_value=builder), \
             patch.object(bot, 'get_db'), patch.object(bot, 'close_db'), \
             patch.object(bot, 'get_poller', return_value=poller), \
             patch.object(bot, 'Bot'):
            bot.run_public_bot()

        post_stop = builder.post_stop.call_args.args[0]
        asyncio.run(post_stop(app))
        poller.stop.assert_called_once()

        post_shutdown = builder.post_shutdown.call_args.args[0]
        asyncio.run(post_shutdown(app))
        poller.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
