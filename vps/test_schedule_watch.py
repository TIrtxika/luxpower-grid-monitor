"""
ScheduleWatch: periodic schedule refresh, reminders and group check.
Run: python -m unittest test_schedule_watch
"""

import unittest
from datetime import date, datetime, timedelta

from schedule import APPLIES, DaySchedule, PlannedOutage
from schedule_watch import ScheduleWatch
from stats import KYIV_TZ


def K(d, h=0, mi=0):
    return datetime(2026, 10, d, h, mi, tzinfo=KYIV_TZ)


def ts(dt):
    return dt.timestamp()


TODAY = DaySchedule(date(2026, 10, 9), APPLIES,
                    (PlannedOutage(K(9, 9), K(9, 12, 30)),))
TOMORROW = DaySchedule(date(2026, 10, 10), APPLIES,
                       (PlannedOutage(K(10, 12), K(10, 15, 30)),))


class FakeDb:
    def __init__(self, stored=()):
        self.saved = []
        self.stored = list(stored)

    def save_schedule(self, group, days, fetched_at):
        self.saved.append((group, list(days), fetched_at))

    def get_schedule(self, group, from_day, to_day):
        return [d for d in self.stored if from_day <= d.day <= to_day]


class Fetcher:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


def make(fetch, db=None, group_check=None):
    db = db or FakeDb()
    w = ScheduleWatch('16.1', fetch, lambda: db, refresh_s=900,
                      lead=timedelta(minutes=30), group_check=group_check,
                      group_check_s=7 * 86400)
    reminders, groups = [], []
    w.add_reminder_callback(reminders.append)
    w.add_group_callback(lambda old, new: groups.append((old, new)))
    return w, db, reminders, groups


class RefreshTest(unittest.TestCase):
    def test_fetches_saves_and_serves_from_today(self):
        fetch = Fetcher([TODAY, TOMORROW])
        w, db, _, _ = make(fetch)
        w.tick(ts(K(9, 8)))
        self.assertEqual(db.saved[0][0], '16.1')
        self.assertEqual(w.days(K(9, 8)), [TODAY, TOMORROW])
        # yesterday's day is dropped after midnight
        self.assertEqual(w.days(K(10, 1)), [TOMORROW])

    def test_refresh_interval(self):
        fetch = Fetcher([TODAY])
        w, _, _, _ = make(fetch)
        w.tick(ts(K(9, 8)))
        w.tick(ts(K(9, 8, 10)))
        self.assertEqual(fetch.calls, 1)
        w.tick(ts(K(9, 8, 15)))
        self.assertEqual(fetch.calls, 2)

    def test_fetch_failure_falls_back_to_db_and_keeps_monitoring(self):
        fetch = Fetcher(OSError('down'))
        w, _, _, _ = make(fetch, db=FakeDb(stored=[TODAY]))
        w.tick(ts(K(9, 8)))  # must not raise
        self.assertEqual(w.days(K(9, 8)), [TODAY])

    def test_failure_keeps_last_good_schedule(self):
        fetch = Fetcher([TODAY, TOMORROW], OSError('down'))
        w, _, _, _ = make(fetch)
        w.tick(ts(K(9, 8)))
        w.tick(ts(K(9, 8, 15)))
        self.assertEqual(w.days(K(9, 8, 15)), [TODAY, TOMORROW])

    def test_empty_answer_does_not_wipe_schedule(self):
        # group missing from the payload -> parse_group returns []
        fetch = Fetcher([TODAY], [])
        w, db, _, _ = make(fetch)
        w.tick(ts(K(9, 8)))
        w.tick(ts(K(9, 8, 15)))
        self.assertEqual(w.days(K(9, 8, 15)), [TODAY])
        self.assertEqual(len(db.saved), 1)


class ReminderTest(unittest.TestCase):
    def test_reminds_once_before_outage(self):
        w, _, reminders, _ = make(Fetcher([TODAY, TOMORROW]))
        w.tick(ts(K(9, 8)))
        w.tick(ts(K(9, 8, 31)))
        w.tick(ts(K(9, 8, 45)))
        self.assertEqual(reminders, [TODAY.outages[0]])

    def test_callback_error_does_not_break_tick(self):
        w, _, reminders, _ = make(Fetcher([TODAY]))

        def boom(_):
            raise RuntimeError('send failed')
        w._reminder_callbacks.insert(0, boom)
        w.tick(ts(K(9, 8, 31)))
        self.assertEqual(reminders, [TODAY.outages[0]])


class GroupCheckTest(unittest.TestCase):
    def test_alerts_once_when_group_changes(self):
        check = Fetcher('16.1', '17.1')
        w, _, _, groups = make(Fetcher([TODAY]), group_check=check)
        w.tick(ts(K(9, 8)))
        self.assertEqual(groups, [])
        w.tick(ts(K(16, 8)))
        w.tick(ts(K(23, 8)))
        self.assertEqual(groups, [('16.1', '17.1')])
        self.assertEqual(check.calls, 3)

    def test_group_check_interval_and_failure(self):
        check = Fetcher(OSError('down'))
        w, _, _, groups = make(Fetcher([TODAY]), group_check=check)
        w.tick(ts(K(9, 8)))
        w.tick(ts(K(9, 9)))
        self.assertEqual((check.calls, groups), (1, []))


if __name__ == '__main__':
    unittest.main()
