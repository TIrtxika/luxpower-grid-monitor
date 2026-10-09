"""
StateTracker tests: on/off/unknown from RPi polls.
Run: python -m unittest test_grid_state
"""

import unittest

from grid_state import (
    StateTracker, ON, OFF, UNKNOWN,
    REASON_RPI_UNREACHABLE, REASON_DONGLE_OFFLINE, REASON_STALE_DATA,
    REASON_NO_GRID_DATA,
)


def st(available=True, connected=True, age=5):
    return {'connected': connected, 'data_age_seconds': age,
            'grid': {'available': available, 'voltage': 230}}


def tracker(**seed):
    return StateTracker(debounce_s=120, unreachable_threshold=3,
                        stale_after_s=180, **seed)


class StateTrackerTest(unittest.TestCase):
    def test_first_observation_sets_state(self):
        t = tracker()
        tr = t.observe(st(True), 0)
        self.assertEqual((tr.state, tr.at, tr.previous, tr.previous_known),
                         (ON, 0, None, None))
        self.assertEqual(t.last_known, ON)

    def test_change_confirmed_after_debounce_at_first_seen(self):
        t = tracker(initial_state=ON, last_known=ON)
        self.assertIsNone(t.observe(st(False), 100))
        self.assertIsNone(t.observe(st(False), 160))
        tr = t.observe(st(False), 220)
        self.assertEqual((tr.state, tr.at, tr.previous, tr.previous_known),
                         (OFF, 100, ON, ON))

    def test_flap_cancels_pending_change(self):
        t = tracker(initial_state=ON, last_known=ON)
        t.observe(st(False), 100)
        self.assertIsNone(t.observe(st(True), 160))
        self.assertIsNone(t.observe(st(False), 220))
        self.assertIsNone(t.observe(st(False), 280))
        self.assertEqual(t.state, ON)

    def test_unreachable_after_threshold_starts_at_first_failure(self):
        t = tracker(initial_state=ON, last_known=ON)
        self.assertIsNone(t.observe(None, 100))
        self.assertIsNone(t.observe(None, 160))
        tr = t.observe(None, 220)
        self.assertEqual((tr.state, tr.at, tr.reason),
                         (UNKNOWN, 100, REASON_RPI_UNREACHABLE))

    def test_dongle_offline_is_unknown_immediately(self):
        t = tracker(initial_state=ON, last_known=ON)
        tr = t.observe(st(True, connected=False), 50)
        self.assertEqual((tr.state, tr.at, tr.reason),
                         (UNKNOWN, 50, REASON_DONGLE_OFFLINE))

    def test_stale_data_is_unknown(self):
        t = tracker(initial_state=OFF, last_known=OFF)
        tr = t.observe(st(False, age=181), 50)
        self.assertEqual((tr.state, tr.reason), (UNKNOWN, REASON_STALE_DATA))

    def test_missing_grid_flag_is_unknown(self):
        t = tracker(initial_state=ON, last_known=ON)
        tr = t.observe(st(None), 50)
        self.assertEqual((tr.state, tr.reason), (UNKNOWN, REASON_NO_GRID_DATA))

    def test_missing_data_age_is_fresh(self):
        t = tracker()
        status = st(True)
        del status['data_age_seconds']
        self.assertEqual(t.observe(status, 0).state, ON)

    def test_leaving_unknown_is_immediate_and_keeps_previous_known(self):
        t = tracker(initial_state=UNKNOWN, last_known=OFF)
        tr = t.observe(st(True), 400)
        self.assertEqual((tr.state, tr.at, tr.previous, tr.previous_known),
                         (ON, 400, UNKNOWN, OFF))

    def test_reason_updates_while_unknown(self):
        t = tracker(initial_state=UNKNOWN, last_known=ON,
                    initial_reason='monitor_downtime')
        for now in (0, 60, 120):
            self.assertIsNone(t.observe(None, now))
        self.assertEqual(t.reason, REASON_RPI_UNREACHABLE)


if __name__ == '__main__':
    unittest.main()
