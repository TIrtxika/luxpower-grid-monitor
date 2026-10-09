"""
Per-subscriber notification settings: quiet hours and notify mode.
Run: python -m unittest test_subscriptions
"""

import unittest
from datetime import datetime, time, timezone

from stats import KYIV_TZ
from subscriptions import (
    QUIET_WINDOWS, Settings, in_quiet_hours, wants, window_key,
)


def at(h, m=0):
    return datetime(2026, 10, 9, h, m, tzinfo=KYIV_TZ)


class QuietHoursTest(unittest.TestCase):
    def test_defaults_are_on_23_to_07(self):
        s = Settings(1)
        self.assertEqual((s.quiet_enabled, s.quiet_from, s.quiet_to, s.notify_mode),
                         (True, time(23), time(7), 'all'))

    def test_window_across_midnight(self):
        s = Settings(1)
        self.assertTrue(in_quiet_hours(s, at(23, 0)))
        self.assertTrue(in_quiet_hours(s, at(3, 15)))
        self.assertTrue(in_quiet_hours(s, at(6, 59)))
        self.assertFalse(in_quiet_hours(s, at(7, 0)))
        self.assertFalse(in_quiet_hours(s, at(22, 59)))

    def test_window_within_one_day(self):
        s = Settings(1, quiet_from=time(0), quiet_to=time(8))
        self.assertTrue(in_quiet_hours(s, at(0, 0)))
        self.assertFalse(in_quiet_hours(s, at(23, 30)))

    def test_disabled(self):
        self.assertFalse(in_quiet_hours(Settings(1, quiet_enabled=False), at(3)))

    def test_uses_kyiv_time_for_aware_datetimes(self):
        # 20:30 UTC = 23:30 Kyiv (EEST): quiet, although the UTC hour is 20
        utc = datetime(2026, 10, 9, 20, 30, tzinfo=timezone.utc)
        self.assertTrue(in_quiet_hours(Settings(1), utc))


class ModeTest(unittest.TestCase):
    def test_modes(self):
        self.assertTrue(wants(Settings(1), grid_on=True))
        self.assertTrue(wants(Settings(1), grid_on=False))
        self.assertTrue(wants(Settings(1, notify_mode='off_only'), grid_on=False))
        self.assertFalse(wants(Settings(1, notify_mode='off_only'), grid_on=True))
        self.assertTrue(wants(Settings(1, notify_mode='on_only'), grid_on=True))
        self.assertFalse(wants(Settings(1, notify_mode='on_only'), grid_on=False))


class WindowKeyTest(unittest.TestCase):
    def test_known_and_custom_windows(self):
        self.assertEqual(window_key(Settings(1)), '23-07')
        self.assertEqual(QUIET_WINDOWS['00-08'], (time(0), time(8)))
        self.assertIsNone(window_key(Settings(1, quiet_from=time(21), quiet_to=time(6))))


if __name__ == '__main__':
    unittest.main()
