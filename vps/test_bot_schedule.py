"""
Planned outage schedule in the bots: /status line, plan vs fact in /stats,
reminder and group-change wiring.
Run: python -m unittest test_bot_schedule
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from schedule import APPLIES, DaySchedule, PlannedOutage
from stats import KYIV_TZ, Interval


def message_update():
    update = MagicMock()
    update.callback_query = None
    update.effective_chat.id = bot.config.OWNER_CHAT_ID
    update.message.reply_text = AsyncMock()
    return update


def upcoming(now, hours=2):
    """Schedule of today with one outage starting in `hours`"""
    start = (now + timedelta(hours=hours)).astimezone(KYIV_TZ).replace(
        minute=0, second=0, microsecond=0)
    return [DaySchedule(start.date(), APPLIES,
                        (PlannedOutage(start, start + timedelta(hours=1)),))]


def poller_with(schedule):
    now = datetime.now(timezone.utc)
    poller = MagicMock()
    poller.get_effective_state.return_value = 'on'
    poller.get_state_reason.return_value = None
    poller.get_state_since.return_value = now - timedelta(minutes=10)
    poller.get_last_status.return_value = {'grid': {'voltage': 230}}
    poller.schedule = schedule
    return poller


class PublicStatusTest(unittest.TestCase):
    def status(self, poller, db=None, group='16.1'):
        update = message_update()
        with patch.object(bot, 'get_poller', return_value=poller), \
             patch.object(bot, 'get_db', return_value=db or MagicMock()), \
             patch.object(bot.config, 'DTEK_GROUP', group):
            asyncio.run(bot.cmd_status(update, MagicMock()))
        return update.message.reply_text.await_args.args[0]

    def test_line_from_poller_memory(self):
        watch = MagicMock()
        watch.days.return_value = upcoming(datetime.now(timezone.utc))
        text = self.status(poller_with(watch))
        self.assertIn("Наступне відключення за графіком", text)

    def test_falls_back_to_db_when_memory_is_empty(self):
        watch = MagicMock()
        watch.days.return_value = []
        db = MagicMock()
        db.get_schedule.return_value = upcoming(datetime.now(timezone.utc))
        text = self.status(poller_with(watch), db)
        self.assertIn("Наступне відключення за графіком", text)
        self.assertEqual(db.get_schedule.call_args.args[0], '16.1')

    def test_no_line_without_group(self):
        text = self.status(poller_with(None), group='')
        self.assertNotIn("графіком", text)

    def test_status_survives_db_failure(self):
        watch = MagicMock()
        watch.days.return_value = []
        db = MagicMock()
        db.get_schedule.side_effect = RuntimeError("db down")
        text = self.status(poller_with(watch), db)
        self.assertIn("Світло є", text)
        self.assertNotIn("графіком", text)


class PrivateStatusTest(unittest.TestCase):
    def test_line_from_db(self):
        now = datetime.now(timezone.utc)
        db = MagicMock()
        db.get_open_interval.return_value = {
            'state': 'on', 'started_at': now - timedelta(hours=1),
            'last_seen_at': now - timedelta(seconds=30)}
        db.get_latest_status.return_value = None
        db.get_schedule.return_value = upcoming(now)
        update = message_update()
        with patch.object(bot, 'get_db', return_value=db), \
             patch.object(bot, 'fetch_status_direct', return_value=None), \
             patch.object(bot.config, 'DTEK_GROUP', '16.1'):
            asyncio.run(bot.cmd_full_status(update, MagicMock()))
        self.assertIn("Наступне відключення за графіком",
                      update.message.reply_text.await_args.args[0])


class PlanFactTest(unittest.TestCase):
    def stats(self, days, intervals, group='16.1'):
        db = MagicMock()
        db.get_intervals.return_value = intervals
        db.get_schedule.return_value = days
        update = message_update()
        with patch.object(bot, 'get_db', return_value=db), \
             patch.object(bot.config, 'DTEK_GROUP', group):
            asyncio.run(bot.cmd_stats(update, MagicMock()))
        return update.message.reply_text.await_args.args[0], db

    def test_plan_vs_fact_since_first_stored_day(self):
        now = datetime.now(timezone.utc)
        first = (now - timedelta(days=2)).astimezone(KYIV_TZ)
        midnight = first.replace(hour=0, minute=0, second=0, microsecond=0)
        days = [DaySchedule(midnight.date(), APPLIES,
                            (PlannedOutage(midnight + timedelta(hours=9),
                                           midnight + timedelta(hours=12, minutes=30)),))]
        intervals = [Interval('on', now - timedelta(days=30), now - timedelta(hours=3)),
                     Interval('off', now - timedelta(hours=3), now - timedelta(hours=1)),
                     Interval('on', now - timedelta(hours=1), now, ongoing=True)]
        text, _ = self.stats(days, intervals)
        self.assertIn(f"План / факт з {midnight:%d.%m}", text)
        self.assertIn("за графіком 3 год 30 хв", text)
        self.assertIn("фактично 2 год", text)

    def test_schedule_not_collected_yet(self):
        text, _ = self.stats([], [])
        self.assertIn("графік ще не зібрано", text)

    def test_no_plan_fact_without_group(self):
        text, db = self.stats([], [], group='')
        self.assertNotIn("План / факт", text)
        db.get_schedule.assert_not_called()


class WiringTest(unittest.TestCase):
    def test_public_bot_wires_schedule_alerts(self):
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
        poller.schedule.add_reminder_callback.assert_called_once_with(
            manager.return_value.on_planned_outage)
        poller.schedule.add_group_callback.assert_called_once_with(
            manager.return_value.on_group_change)


if __name__ == '__main__':
    unittest.main()
