"""
Poller: interval recording, grid change callbacks, unknown alerts.
Run: python -m unittest test_poller
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import poller as poller_mod
from grid_state import GridChange
from poller import RpiPoller

HC_URL = "https://hc-ping.com/00000000-test-uuid"


def dt(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def st(available=True, connected=True, soc=None):
    return {'connected': connected, 'data_age_seconds': 5,
            'grid': {'available': available, 'voltage': 230},
            'battery': {'soc': soc}}


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
        self.fail_init = 0
        self.heartbeat_rows = 1

    def recover_gap(self, now, max_gap):
        if self.fail_init:
            self.fail_init -= 1
            raise RuntimeError("db down at startup")
        return self.gap

    def get_open_interval(self):
        return self.open_iv

    def get_last_known_state(self):
        return self.last_known

    def heartbeat(self, now):
        self.heartbeats.append(now)
        return self.heartbeat_rows

    def switch_state(self, state, at, now):
        if self.fail_switch:
            self.fail_switch -= 1
            raise RuntimeError("db down")
        closed = None
        boundary = at
        if self.open_iv:
            # Like the real DB: the boundary never precedes the open interval start
            boundary = max(at, self.open_iv['started_at'])
            closed = {'state': self.open_iv['state'],
                      'started_at': self.open_iv['started_at'], 'ended_at': boundary}
        self.switches.append((state, at))
        self.open_iv = {'state': state, 'started_at': boundary, 'last_seen_at': now}
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
        self.battery = []
        self.poller.add_battery_callback(
            lambda level, soc, fc: self.battery.append((level, soc, fc)))
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

    def test_quick_restart_during_unknown_does_not_realert(self):
        h = Harness(seeded('unknown', started=-3600))
        for t in (0, 60, 120, 180):
            h.poll(t, None)
        self.assertEqual(h.unknown, [])

    def test_restart_after_gap_waits_full_threshold_before_unknown_alert(self):
        db = FakeDb(open_iv={'state': 'unknown', 'started_at': dt(-7200),
                             'last_seen_at': dt(-3600)},
                    last_known='on', gap=(dt(-3600), dt(0)))
        h = Harness(db)
        for t in (0, 60, 120, 180, 240):
            h.poll(t, None)
        self.assertEqual(h.unknown, [(False, 'monitor_downtime', 3600)])
        h.poll(300, None)
        self.assertEqual(h.unknown[-1], (True, 'rpi_unreachable', 7500))

    def test_vanished_open_interval_is_reopened(self):
        db = seeded('on')
        db.heartbeat_rows = 0  # e.g. migration --force wiped the table
        h = Harness(db)
        h.poll(0, st(True))
        self.assertEqual(db.switches, [('on', dt(-1000))])

    def test_low_battery_alerts_once_per_threshold_with_forecast(self):
        h = Harness(seeded('off'))
        for i, soc in enumerate([40, 38, 36, 34, 32, 31, 30, 29]):
            h.poll(i * 120, st(False, soc=soc))
        self.assertEqual([(lvl, soc) for lvl, soc, _ in h.battery], [(30, 30)])
        fc = h.battery[0][2]
        self.assertIsNotNone(fc)
        self.assertGreater(fc.rate_per_hour, 0)

    def test_no_battery_alert_while_grid_on(self):
        h = Harness(seeded('on'))
        h.poll(0, st(True, soc=10))
        self.assertEqual(h.battery, [])

    def test_battery_thresholds_rearm_after_grid_returns(self):
        h = Harness(seeded('off'))
        h.poll(0, st(False, soc=25))
        for t in (60, 120, 180):
            h.poll(t, st(True, soc=26))
        for t in (240, 300, 360, 420):
            h.poll(t, st(False, soc=28))
        self.assertEqual([lvl for lvl, _, _ in h.battery], [30, 30])

    def test_watchdog_pinged_every_poll_even_when_rpi_down(self):
        h = Harness(seeded('on'))
        with patch.object(poller_mod.config, 'HEALTHCHECK_URL', HC_URL), \
             patch.object(poller_mod.requests, 'get',
                          return_value=MagicMock(status_code=200)) as get:
            h.poll(0, st(True))
            h.poll(60, None)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.args[0], HC_URL)
        self.assertEqual(get.call_args.kwargs['timeout'], 5)

    def test_watchdog_disabled_without_url(self):
        h = Harness(seeded('on'))
        with patch.object(poller_mod.config, 'HEALTHCHECK_URL', ''), \
             patch.object(poller_mod.requests, 'get') as get:
            h.poll(0, st(True))
        get.assert_not_called()

    def test_watchdog_failure_is_logged_without_url(self):
        h = Harness(seeded('on'))
        with patch.object(poller_mod.config, 'HEALTHCHECK_URL', HC_URL), \
             patch.object(poller_mod.requests, 'get',
                          side_effect=poller_mod.requests.ConnectionError(HC_URL)), \
             self.assertLogs('poller', level='WARNING') as logs:
            h.poll(0, st(True))
        self.assertNotIn(HC_URL, "\n".join(logs.output))

    def test_outage_duration_never_negative_after_clock_step(self):
        # the off interval in the DB starts after the boundary the tracker reports
        h = Harness(seeded('off', started=500))
        for t in (0, 60, 120):
            h.poll(t, st(True))
        self.assertEqual(h.changes[0].duration_s, 0)

    def test_init_retried_when_db_was_down_at_startup(self):
        db = FakeDb(open_iv={'state': 'unknown', 'started_at': dt(-3600),
                             'last_seen_at': dt(-3600)},
                    last_known='on', gap=(dt(-3600), dt(0)))
        db.fail_init = 1
        h = Harness(db)
        self.assertEqual(h.unknown, [])
        h.poll(60, st(True))
        self.assertEqual(h.unknown, [(False, 'monitor_downtime', 3600)])

    def test_state_since_in_memory(self):
        h = Harness(seeded('on'))
        for t in (0, 60, 120):
            h.poll(t, st(False))
        self.assertEqual(h.poller.get_state_since(), dt(0))

    def test_empty_db_first_poll_opens_interval_without_alert(self):
        h = Harness(FakeDb())
        h.poll(0, st(True))
        self.assertEqual(h.db.switches, [('on', dt(0))])
        self.assertEqual(h.changes, [])


if __name__ == '__main__':
    unittest.main()
