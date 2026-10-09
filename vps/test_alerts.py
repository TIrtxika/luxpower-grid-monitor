"""
AlertManager: save-first, blocked subscribers, owner detail, unknown alerts.
Run: python -m unittest test_alerts
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import Forbidden, TelegramError

import alerts
from alerts import AlertManager
from grid_state import GridChange

STATUS = {'grid': {'available': True, 'voltage': 231},
          'battery': {'soc': 80}, 'output': {}, 'temperature': {}}


def bot(side_effect=None):
    b = MagicMock()
    b.send_message = AsyncMock(side_effect=side_effect)
    return b


class GridAlertTest(unittest.TestCase):
    def run_alert(self, am, db, change):
        with patch.object(alerts, 'get_db', return_value=db), \
             patch.object(alerts.config, 'OWNER_CHAT_ID', 42), \
             patch.object(alerts.config, 'PUBLIC_CHANNEL_ID', ''):
            asyncio.run(am.send_grid_alert(change, STATUS))

    def test_event_saved_before_sending(self):
        calls = []
        db = MagicMock()
        db.save_event.side_effect = lambda *a, **k: calls.append('save')
        db.get_active_subscribers.return_value = [1]
        public = bot(side_effect=lambda **k: calls.append('send'))
        self.run_alert(AlertManager(public), db, GridChange('off', 0, None, False))
        self.assertEqual(calls, ['save', 'send'])

    def test_event_saved_even_if_sending_fails(self):
        db = MagicMock()
        db.get_active_subscribers.return_value = [1, 2]
        public = bot(side_effect=TelegramError("boom"))
        self.run_alert(AlertManager(public), db, GridChange('off', 0, None, False))
        db.save_event.assert_called_once()
        self.assertEqual(public.send_message.await_count, 2)

    def test_blocked_subscriber_is_deactivated(self):
        db = MagicMock()
        db.get_active_subscribers.return_value = [1, 2]
        public = bot(side_effect=[Forbidden("bot was blocked by the user"), None])
        self.run_alert(AlertManager(public), db, GridChange('on', 0, 600, False))
        db.remove_subscriber.assert_called_once_with(1)

    def test_owner_gets_detail_via_private_bot(self):
        db = MagicMock()
        db.get_active_subscribers.return_value = []
        private = bot()
        self.run_alert(AlertManager(bot(), private), db,
                       GridChange('on', 0, 600, False))
        private.send_message.assert_awaited_once()
        self.assertEqual(private.send_message.await_args.kwargs['chat_id'], 42)

    def test_message_has_duration_and_approximate_note(self):
        am = AlertManager(bot())
        text = am._format_grid_message(True, STATUS, 3900, approximate=True)
        self.assertIn("1 год 5 хв", text)
        self.assertIn("приблизний", text)


class UnknownAlertTest(unittest.TestCase):
    def send(self, *args):
        private = bot()
        with patch.object(alerts.config, 'OWNER_CHAT_ID', 42):
            asyncio.run(AlertManager(bot(), private)._send_unknown_alert(*args))
        return private.send_message.await_args.kwargs['text']

    def test_unknown_alert_has_reason(self):
        text = self.send(True, 'dongle_offline', 300)
        self.assertIn("WiFi-донгл", text)
        self.assertIn("5 хв", text)

    def test_restored_alert(self):
        self.assertIn("знову надходять", self.send(False, None, 600))

    def test_monitor_downtime_alert(self):
        self.assertIn("Моніторинг не працював", self.send(False, 'monitor_downtime', 7200))


class RunAsyncTest(unittest.TestCase):
    def test_uses_threadsafe_dispatch_when_loop_running(self):
        loop = MagicMock()
        loop.is_running.return_value = True
        am = AlertManager(bot())
        am.set_event_loop(loop)

        async def noop():
            pass

        coro = noop()
        with patch.object(alerts.asyncio, 'run_coroutine_threadsafe') as m:
            am._run_async(coro)
        m.assert_called_once_with(coro, loop)
        coro.close()

    def test_uses_threadsafe_dispatch_between_loop_runs(self):
        # run_polling drives the loop with separate run_until_complete calls;
        # between them is_running() is False but the loop is still the bot's
        loop = MagicMock()
        loop.is_running.return_value = False
        am = AlertManager(bot())
        am.set_event_loop(loop)

        async def noop():
            pass

        coro = noop()
        with patch.object(alerts.asyncio, 'run_coroutine_threadsafe') as m, \
             patch.object(alerts.asyncio, 'run') as run:
            am._run_async(coro)
        m.assert_called_once_with(coro, loop)
        run.assert_not_called()
        coro.close()

    def test_failed_background_send_is_logged(self):
        future = MagicMock()
        future.cancelled.return_value = False
        future.exception.return_value = RuntimeError("http client closed")
        with self.assertLogs('alerts', level='ERROR') as logs:
            alerts._log_future_error(future)
        self.assertIn("http client closed", logs.output[0])


class FakeChannel:
    def __init__(self, fail=False):
        self.notices = []
        self.fail = fail

    def send(self, notice):
        if self.fail:
            raise RuntimeError("ntfy down")
        self.notices.append(notice)
        return True


class OwnerChannelsTest(unittest.TestCase):
    def grid(self, change, channel, subscribers=()):
        db = MagicMock()
        db.get_active_subscribers.return_value = list(subscribers)
        public = bot()
        am = AlertManager(public, owner_channels=[channel])
        with patch.object(alerts, 'get_db', return_value=db), \
             patch.object(alerts.config, 'OWNER_CHAT_ID', 42), \
             patch.object(alerts.config, 'PUBLIC_CHANNEL_ID', ''):
            asyncio.run(am.send_grid_alert(change, STATUS))
        return public

    def unknown(self, *args):
        channel = FakeChannel()
        with patch.object(alerts.config, 'OWNER_CHAT_ID', 42):
            asyncio.run(AlertManager(bot(), bot(), owner_channels=[channel])
                        ._send_unknown_alert(*args))
        return channel.notices[0]

    def test_grid_off_is_high_priority(self):
        channel = FakeChannel()
        self.grid(GridChange('off', 0, None, False), channel)
        n = channel.notices[0]
        self.assertEqual((n.title, n.priority, n.tags),
                         ("Світла немає", 4, ('red_circle',)))

    def test_grid_on_has_duration(self):
        channel = FakeChannel()
        self.grid(GridChange('on', 0, 3900, False), channel)
        n = channel.notices[0]
        self.assertEqual((n.title, n.priority, n.tags),
                         ("Світло є", 3, ('green_circle',)))
        self.assertIn("1 год 5 хв", n.message)

    def test_unknown_states(self):
        active = self.unknown(True, 'stale_data', 300)
        self.assertEqual((active.priority, active.tags), (3, ('yellow_circle',)))
        self.assertIn("дані застарілі", active.message)
        restored = self.unknown(False, None, 600)
        self.assertEqual((restored.priority, restored.tags), (2, ('white_check_mark',)))
        downtime = self.unknown(False, 'monitor_downtime', 7200)
        self.assertEqual((downtime.priority, downtime.tags), (3, ('warning',)))

    def low_battery(self, level, soc, fc):
        channel = FakeChannel()
        private = bot()
        with patch.object(alerts.config, 'OWNER_CHAT_ID', 42):
            asyncio.run(AlertManager(bot(), private, owner_channels=[channel])
                        ._send_low_battery(level, soc, fc))
        return channel.notices[0], private.send_message.await_args.kwargs['text']

    def test_low_battery_warning_with_forecast(self):
        from battery import Forecast
        n, text = self.low_battery(30, 29, Forecast(29, 18.0, 6000))
        self.assertEqual((n.priority, n.tags), (4, ('battery',)))
        self.assertEqual(n.title, "Батарея 29%")
        self.assertIn("1 год 40 хв", n.message)
        self.assertIn("1 год 40 хв", text)

    def test_critical_battery_is_urgent(self):
        n, _ = self.low_battery(15, 14, None)
        self.assertEqual((n.priority, n.tags), (5, ('rotating_light',)))
        self.assertIn("прогноз ще недоступний", n.message)

    def test_channel_failure_does_not_stop_telegram(self):
        public = self.grid(GridChange('off', 0, None, False), FakeChannel(fail=True),
                           subscribers=[1, 2])
        self.assertEqual(public.send_message.await_count, 2)


if __name__ == '__main__':
    unittest.main()
