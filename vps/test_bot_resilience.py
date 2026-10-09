"""
/status uses the poller's in-memory state; commands answer when the DB is down;
matplotlib keeps its cache in a fixed app directory.
Run: python -m unittest test_bot_resilience
"""

import asyncio
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2

import bot

VPS_DIR = Path(__file__).resolve().parent


def message_update():
    update = MagicMock()
    update.callback_query = None
    update.effective_chat.id = bot.config.OWNER_CHAT_ID
    update.message.reply_text = AsyncMock()
    return update


class StatusSinceTest(unittest.TestCase):
    def test_since_comes_from_poller_memory(self):
        now = datetime.now(timezone.utc)
        poller = MagicMock()
        poller.get_effective_state.return_value = 'off'
        poller.get_state_reason.return_value = None
        poller.get_state_since.return_value = now - timedelta(minutes=10)
        poller.get_last_status.return_value = {'grid': {'voltage': 0}}
        db = MagicMock()
        # stale DB row (e.g. a failed write): must not be used
        db.get_open_interval.return_value = {'state': 'on',
                                             'started_at': now - timedelta(hours=5)}
        update = message_update()
        with patch.object(bot, 'get_poller', return_value=poller), \
             patch.object(bot, 'get_db', return_value=db):
            asyncio.run(bot.cmd_status(update, MagicMock()))
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("(10 хв)", text)
        self.assertNotIn("5 год", text)


class DbDownTest(unittest.TestCase):
    def run_with_db_down(self, handler, update):
        with patch.object(bot, 'get_db',
                          side_effect=psycopg2.OperationalError("connection refused")):
            asyncio.run(handler(update, MagicMock()))

    def test_commands_reply_when_db_is_down(self):
        for handler in (bot.cmd_grid, bot.cmd_history, bot.cmd_stats, bot.cmd_settings):
            update = message_update()
            self.run_with_db_down(handler, update)
            text = update.message.reply_text.await_args.args[0]
            self.assertIn("недоступна", text, handler.__name__)

    def test_callbacks_answer_when_db_is_down(self):
        for handler in (bot.callback_grid, bot.callback_history_detail):
            query = MagicMock()
            query.data = 'grid_week'
            query.answer = AsyncMock()
            update = MagicMock(callback_query=query)
            self.run_with_db_down(handler, update)
            texts = [c.args[0] for c in query.answer.await_args_list if c.args]
            self.assertTrue(any("недоступна" in t for t in texts), handler.__name__)


class MatplotlibConfigDirTest(unittest.TestCase):
    def test_uses_fixed_app_config_dir(self):
        env = {k: v for k, v in os.environ.items() if k != 'MPLCONFIGDIR'}
        result = subprocess.run(
            [sys.executable, '-c',
             'import graphs, matplotlib; print(matplotlib.get_configdir())'],
            cwd=VPS_DIR, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), str(VPS_DIR / '.mplconfig'),
                         result.stderr)


if __name__ == '__main__':
    unittest.main()
