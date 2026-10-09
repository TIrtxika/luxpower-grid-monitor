"""
Kyiv time display tests: the bot must follow Europe/Kyiv DST, not a fixed offset.
Run: python -m unittest test_timezone
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import alerts
import bot
from stats import Interval

SUMMER_UTC = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
WINTER_UTC = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)


class KyivTzTest(unittest.TestCase):
    def test_bot_tz_is_utc_plus_3_in_summer(self):
        self.assertEqual(SUMMER_UTC.astimezone(bot.KYIV_TZ).utcoffset(), timedelta(hours=3))

    def test_bot_tz_is_utc_plus_2_in_winter(self):
        self.assertEqual(WINTER_UTC.astimezone(bot.KYIV_TZ).utcoffset(), timedelta(hours=2))

    def test_alerts_tz_is_utc_plus_3_in_summer(self):
        self.assertEqual(SUMMER_UTC.astimezone(alerts.KYIV_TZ).utcoffset(), timedelta(hours=3))

    def test_alerts_tz_is_utc_plus_2_in_winter(self):
        self.assertEqual(WINTER_UTC.astimezone(alerts.KYIV_TZ).utcoffset(), timedelta(hours=2))


class HistoryDetailTest(unittest.TestCase):
    def test_history_shows_kyiv_time_for_utc_timestamp(self):
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) \
            - timedelta(hours=3)
        db = MagicMock()
        db.get_intervals.return_value = [
            Interval('off', start, start + timedelta(hours=1)),
        ]
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock(callback_query=query)

        with patch.object(bot, 'get_db', return_value=db):
            asyncio.run(bot.callback_history_detail(update, MagicMock()))

        text = query.edit_message_text.call_args.args[0]
        expected = start.astimezone(bot.KYIV_TZ).strftime('%H:%M %d.%m')
        self.assertIn(expected, text)


if __name__ == '__main__':
    unittest.main()
