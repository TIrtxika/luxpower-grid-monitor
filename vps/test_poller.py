"""
Poller: interval recording, grid change callbacks, unknown alerts.
Run: python -m unittest test_poller
"""

import unittest
from datetime import datetime, timezone

from grid_state import GridChange
from poller import RpiPoller


def dt(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def st(available=True, connected=True):
    return {'connected': connected, 'data_age_seconds': 5,
            'grid': {'available': available, 'voltage': 230}}


class FakeDb:
    def __init__(self, open_iv=None, last_known=None, gap=None):
        self.open_iv = open_iv
        self.last_known = last_known
        self.gap = gap
        self.switches = []
        self.heartbeats = []
        self.saved = []
        self.cleanups = []
        self.fail_switch = 0

    def recover_gap(self, now, max_gap):
        return self.gap

    def get_open_interval(self):
        return self.open_iv

    def get_last_known_state(self):
        return self.last_known

    def heartbeat(self, now):
        self.heartbeats.append(now)

    def switch_state(self, state, at, now):
        if self.fail_switch:
            self.fail_switch -= 1
            raise RuntimeError("db down")
        closed = None
        if self.open_iv:
            closed = {'state': self.open_iv['state'],
                      'started_at': self.open_iv['started_at'], 'ended_at': at}
        self.switches.append((state, at))
        self.open_iv = {'state': state, 'started_at': at, 'last_seen_at': now}
        return closed

    def save_status(self, status):
        self.saved.append(status)

    def cleanup_status(self, days):
        self.cleanups.append(days)
        return 0


class Harness:
    def __init__(self, db):
        self.now = 0.0
        self.db = db
        self.poller = RpiPoller(clock=lambda: self.now, db_factory=lambda: db)
        self.changes = []
        self.unknown = []
        self.poller.add_state_callback(lambda c, s: self.changes.append(c))
        self.poller.add_unknown_callback(
            lambda a, r, sec: self.unknown.append((a, r, sec)))
        self.poller.init_state()

    def poll(self, t, status):
        self.now = t
        self.poller.fetch_status = lambda: status
        self.poller._poll_once()


def seeded(state, started=-1000, **kw):
    return FakeDb(open_iv={'state': state, 'started_at': dt(started),
                           'last_seen_at': dt(0)}, last_known=state, **kw)


class PollerTest(unittest.TestCase):
    def test_off_change_after_debounce_records_boundary(self):
        h = Harness(seeded('on'))
        for t in (0, 60, 120):
            h.poll(t, st(False))
        self.assertEqual(h.db.switches, [('off', dt(0))])
        self.assertEqual(h.changes, [GridChange('off', 0, None, False)])

    def test_on_after_off_reports_outage_duration(self):
        h = Harness(seeded('off', started=-600))
        for t in (0, 60, 120):
            h.poll(t, st(True))
        self.assertEqual(h.changes, [GridChange('on', 0, 600, False)])

    def test_unknown_alert_after_threshold_and_restore(self):
        h = Harness(seeded('on'))
        for t in (0, 60, 120):
            h.poll(t, None)
        self.assertEqual(h.unknown, [])
        h.poll(300, None)
        self.assertEqual(h.unknown, [(True, 'rpi_unreachable', 300)])
        h.poll(360, st(True))
        self.assertEqual(h.unknown[-1], (False, None, 360))
        self.assertEqual(h.changes, [])
        self.assertEqual(h.db.switches, [('unknown', dt(0)), ('on', dt(360))])

    def test_change_during_unknown_is_approximate(self):
        h = Harness(seeded('on'))
        h.poll(0, st(True, connected=False))
        h.poll(60, st(False))
        self.assertEqual(h.changes, [GridChange('off', 60, None, True)])

    def test_startup_gap_reports_monitor_downtime(self):
        db = FakeDb(open_iv={'state': 'unknown', 'started_at': dt(-3600),
                             'last_seen_at': dt(0)},
                    last_known='on', gap=(dt(-3600), dt(0)))
        h = Harness(db)
        self.assertEqual(h.unknown, [(False, 'monitor_downtime', 3600)])
        self.assertEqual(h.poller.get_effective_state(), 'unknown')
        h.poll(60, st(True))
        self.assertEqual(h.changes, [])
        self.assertEqual(len(h.unknown), 1)

    def test_quick_restart_only_heartbeats(self):
        h = Harness(seeded('on'))
        h.poll(0, st(True))
        self.assertEqual(h.db.switches, [])
        self.assertEqual(h.db.heartbeats, [dt(0)])
        self.assertEqual((h.changes, h.unknown), ([], []))

    def test_db_failure_does_not_block_alert_and_is_retried(self):
        db = seeded('on')
        db.fail_switch = 1
        h = Harness(db)
        for t in (0, 60, 120):
            h.poll(t, st(False))
        self.assertEqual(len(h.changes), 1)
        self.assertEqual(db.switches, [])
        h.poll(180, st(False))
        self.assertEqual(db.switches, [('off', dt(0))])

    def test_retention_runs_once_a_day(self):
        h = Harness(seeded('on'))
        h.poll(0, st(True))
        h.poll(60, st(True))
        self.assertEqual(h.db.cleanups, [90])
        h.poll(86400, st(True))
        self.assertEqual(h.db.cleanups, [90, 90])

    def test_empty_db_first_poll_opens_interval_without_alert(self):
        h = Harness(FakeDb())
        h.poll(0, st(True))
        self.assertEqual(h.db.switches, [('on', dt(0))])
        self.assertEqual(h.changes, [])


if __name__ == '__main__':
    unittest.main()
