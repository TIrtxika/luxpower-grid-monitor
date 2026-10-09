"""
Backfill migration: inverter_status samples -> grid_intervals.
Run: python -m unittest test_migrate_intervals
"""

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from migrate_intervals import main, samples_to_intervals

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def T(minutes):
    return BASE + timedelta(minutes=minutes)


def s(minutes, grid=True, connected=True):
    return {'timestamp': T(minutes), 'connected': connected, 'grid_available': grid}


class SamplesToIntervalsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(samples_to_intervals([]), [])

    def test_merges_same_state_and_splits_on_change(self):
        ivs = samples_to_intervals([s(0), s(5), s(10, False), s(15, False), s(20)])
        self.assertEqual([(iv['state'], iv['started_at'], iv['ended_at']) for iv in ivs],
                         [('on', T(0), T(10)), ('off', T(10), T(20))])

    def test_gap_becomes_unknown(self):
        ivs = samples_to_intervals([s(0), s(5), s(60), s(65)])
        self.assertEqual([(iv['state'], iv['started_at'], iv['ended_at']) for iv in ivs],
                         [('on', T(0), T(5)), ('unknown', T(5), T(60)),
                          ('on', T(60), T(65))])

    def test_disconnected_or_null_is_unknown(self):
        ivs = samples_to_intervals([s(0), s(5, connected=False), s(10, grid=None),
                                    s(15), s(20)])
        self.assertEqual([iv['state'] for iv in ivs], ['on', 'unknown', 'on'])


class MainTest(unittest.TestCase):
    def test_main_refuses_when_intervals_exist(self):
        db = MagicMock()
        db.count_intervals.return_value = 5
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([], db=db), 1)
        db.insert_intervals.assert_not_called()

    def test_dry_run_writes_nothing(self):
        db = MagicMock()
        db.count_intervals.return_value = 0
        db.get_state_samples.return_value = [s(0), s(5), s(10, False), s(15, False)]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['--dry-run'], db=db), 0)
        db.insert_intervals.assert_not_called()
        self.assertIn('intervals: 2', out.getvalue())

    def test_force_replaces_existing(self):
        db = MagicMock()
        db.count_intervals.return_value = 5
        db.get_state_samples.return_value = [s(0), s(5)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--force'], db=db), 0)
        db.truncate_intervals.assert_called_once()
        db.insert_intervals.assert_called_once()


if __name__ == '__main__':
    unittest.main()
