"""
grid_intervals DB layer (integration, needs PostgreSQL).
Run: TEST_DATABASE_URL=postgresql://... python -m unittest test_database_intervals
"""

import os
import unittest
from datetime import datetime, time, timedelta, timezone

from database import Database

TEST_URL = os.environ.get('TEST_DATABASE_URL')
BASE = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def T(minutes):
    return BASE + timedelta(minutes=minutes)


@unittest.skipUnless(TEST_URL, "TEST_DATABASE_URL not set")
class GridIntervalsDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database(TEST_URL)
        cls.db.connect()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE grid_intervals, inverter_status RESTART IDENTITY")

    def _open_count(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM grid_intervals WHERE ended_at IS NULL")
                return cur.fetchone()[0]

    def test_first_switch_opens_interval(self):
        self.assertIsNone(self.db.switch_state('on', T(0), T(0)))
        open_iv = self.db.get_open_interval()
        self.assertEqual((open_iv['state'], open_iv['started_at']), ('on', T(0)))

    def test_switch_closes_previous(self):
        self.db.switch_state('on', T(0), T(0))
        closed = self.db.switch_state('off', T(10), T(12))
        self.assertEqual(closed, {'state': 'on', 'started_at': T(0), 'ended_at': T(10)})
        ivs = self.db.get_intervals(T(-60), T(60))
        self.assertEqual([(iv.state, iv.start, iv.ongoing) for iv in ivs],
                         [('on', T(0), False), ('off', T(10), True)])
        self.assertEqual(ivs[0].end, T(10))

    def test_boundary_before_open_start_is_clamped(self):
        self.db.switch_state('on', T(10), T(10))
        closed = self.db.switch_state('off', T(5), T(11))
        self.assertEqual(closed['ended_at'], T(10))
        self.assertEqual(self.db.get_open_interval()['started_at'], T(10))

    def test_only_one_open_interval(self):
        self.db.switch_state('on', T(0), T(0))
        self.db.switch_state('off', T(1), T(1))
        self.db.switch_state('unknown', T(2), T(2))
        self.assertEqual(self._open_count(), 1)

    def test_same_state_switch_is_heartbeat(self):
        self.db.switch_state('on', T(0), T(0))
        self.assertIsNone(self.db.switch_state('on', T(5), T(5)))
        self.assertEqual(self.db.get_open_interval()['last_seen_at'], T(5))

    def test_heartbeat_updates_last_seen(self):
        self.db.switch_state('on', T(0), T(0))
        self.db.heartbeat(T(3))
        self.assertEqual(self.db.get_open_interval()['last_seen_at'], T(3))

    def test_recover_gap_records_unknown(self):
        self.db.switch_state('on', T(0), T(0))
        self.db.heartbeat(T(1))
        gap = self.db.recover_gap(T(30), timedelta(minutes=3))
        self.assertEqual(gap, (T(1), T(30)))
        open_iv = self.db.get_open_interval()
        self.assertEqual((open_iv['state'], open_iv['started_at']), ('unknown', T(1)))
        ivs = self.db.get_intervals(T(-60), T(60))
        self.assertEqual((ivs[0].state, ivs[0].end), ('on', T(1)))

    def test_recover_gap_without_gap(self):
        self.db.switch_state('on', T(0), T(0))
        self.assertIsNone(self.db.recover_gap(T(2), timedelta(minutes=3)))
        self.assertEqual(self.db.get_open_interval()['state'], 'on')

    def test_last_known_skips_unknown(self):
        self.db.switch_state('on', T(0), T(0))
        self.db.switch_state('unknown', T(5), T(5))
        self.assertEqual(self.db.get_last_known_state(), 'on')

    def test_cleanup_status_deletes_old_rows(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO inverter_status (timestamp, connected)
                    VALUES (NOW() - INTERVAL '100 days', TRUE),
                           (NOW() - INTERVAL '1 day', TRUE)
                """)
        self.assertEqual(self.db.cleanup_status(90), 1)

    def test_migration_helpers(self):
        self.db.insert_intervals([
            {'state': 'on', 'started_at': T(0), 'ended_at': T(5)},
            {'state': 'off', 'started_at': T(5), 'ended_at': T(9)},
        ])
        self.assertEqual(self.db.count_intervals(), 2)
        self.assertIsNone(self.db.get_open_interval())
        self.db.replace_intervals([
            {'state': 'unknown', 'started_at': T(0), 'ended_at': T(9)},
        ])
        self.assertEqual(self.db.count_intervals(), 1)

    def test_failed_replace_keeps_old_rows(self):
        self.db.insert_intervals([
            {'state': 'on', 'started_at': T(0), 'ended_at': T(5)},
        ])
        with self.assertRaises(Exception):
            self.db.replace_intervals([
                {'state': 'bogus', 'started_at': T(0), 'ended_at': T(5)},
            ])
        self.assertEqual(self.db.count_intervals(), 1)

    def test_heartbeat_reports_missing_open_interval(self):
        self.assertEqual(self.db.heartbeat(T(0)), 0)
        self.db.switch_state('on', T(0), T(0))
        self.assertEqual(self.db.heartbeat(T(1)), 1)

    def test_state_samples(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO inverter_status (timestamp, connected, grid_available)
                    VALUES (%s, TRUE, TRUE), (%s, FALSE, NULL)
                """, (T(5), T(0)))
        samples = self.db.get_state_samples()
        self.assertEqual([(s['timestamp'], s['connected'], s['grid_available'])
                          for s in samples],
                         [(T(0), False, None), (T(5), True, True)])


@unittest.skipUnless(TEST_URL, "TEST_DATABASE_URL not set")
class SubscriberSettingsDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database(TEST_URL)
        cls.db.connect()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE subscribers RESTART IDENTITY")

    def test_new_subscriber_gets_quiet_hours_by_default(self):
        self.db.add_subscriber(1, 'a')
        s = self.db.get_settings(1)
        self.assertEqual((s.quiet_enabled, s.quiet_from, s.quiet_to, s.notify_mode),
                         (True, time(23), time(7), 'all'))

    def test_update_settings(self):
        self.db.add_subscriber(1, 'a')
        self.db.update_settings(1, quiet_enabled=False, notify_mode='off_only',
                                quiet_from=time(22), quiet_to=time(7))
        s = self.db.get_settings(1)
        self.assertEqual((s.quiet_enabled, s.notify_mode, s.quiet_from),
                         (False, 'off_only', time(22)))

    def test_update_rejects_unknown_fields_and_modes(self):
        self.db.add_subscriber(1, 'a')
        with self.assertRaises(ValueError):
            self.db.update_settings(1, is_active=False)
        with self.assertRaises(Exception):
            self.db.update_settings(1, notify_mode='sometimes')

    def test_settings_of_active_subscribers_only(self):
        self.db.add_subscriber(1, 'a')
        self.db.add_subscriber(2, 'b')
        self.db.remove_subscriber(2)
        self.assertEqual([s.chat_id for s in self.db.get_subscriber_settings()], [1])

    def test_unknown_chat_has_no_settings(self):
        self.assertIsNone(self.db.get_settings(999))

    def test_migration_is_idempotent_for_existing_rows(self):
        self.db.add_subscriber(1, 'a')
        self.db._create_tables()
        self.assertEqual(self.db.get_settings(1).quiet_from, time(23))


if __name__ == '__main__':
    unittest.main()
