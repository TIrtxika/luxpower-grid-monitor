"""
Statistics over grid intervals (Kyiv calendar, DST, unknown gaps).
Run: python -m unittest test_stats
"""

import unittest
from datetime import date, datetime, timedelta

from stats import (
    KYIV_TZ, Interval, Outage, availability_pct, hourly_heatmap,
    outage_summary, outages, period_bounds, split_by_periods, window_stats,
)

H = 3600


def K(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=KYIV_TZ)


class SplitByPeriodsTest(unittest.TestCase):
    def test_day_split_counts_uncovered_time_as_unknown(self):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('on', K(2026, 10, 9, 0), K(2026, 10, 9, 10)),
               Interval('off', K(2026, 10, 9, 10), now, ongoing=True)]
        yesterday, today = split_by_periods(ivs, 'day', 2, now)
        self.assertEqual((yesterday.on, yesterday.off, yesterday.unknown),
                         (0, 0, 24 * H))
        self.assertFalse(yesterday.partial)
        self.assertEqual((today.on, today.off, today.unknown),
                         (10 * H, 2 * H, 0))
        self.assertTrue(today.partial)
        self.assertEqual(today.start, K(2026, 10, 9))

    def test_spring_dst_day_has_23_hours(self):
        now = K(2026, 3, 30, 12)
        ivs = [Interval('on', K(2026, 3, 28), now, ongoing=True)]
        days = split_by_periods(ivs, 'day', 3, now)
        self.assertEqual(days[1].on, 23 * H)

    def test_autumn_dst_day_has_25_hours(self):
        now = K(2026, 10, 26, 12)
        ivs = [Interval('on', K(2026, 10, 24), now, ongoing=True)]
        days = split_by_periods(ivs, 'day', 3, now)
        self.assertEqual(days[1].on, 25 * H)

    def test_outage_across_midnight_is_split_between_days(self):
        now = K(2026, 10, 8, 12)
        ivs = [Interval('on', K(2026, 10, 7), K(2026, 10, 7, 22)),
               Interval('off', K(2026, 10, 7, 22), K(2026, 10, 8, 2)),
               Interval('on', K(2026, 10, 8, 2), now, ongoing=True)]
        d7, d8 = split_by_periods(ivs, 'day', 2, now)
        self.assertEqual((d7.on, d7.off), (22 * H, 2 * H))
        self.assertEqual((d8.on, d8.off), (10 * H, 2 * H))

    def test_week_starts_on_monday(self):
        now = K(2026, 10, 8, 12)  # Thursday
        self.assertEqual(period_bounds('week', 2, now),
                         [(K(2026, 9, 28), K(2026, 10, 5), False),
                          (K(2026, 10, 5), now, True)])

    def test_twelve_months(self):
        bounds = period_bounds('month', 12, K(2026, 10, 9, 12))
        self.assertEqual(len(bounds), 12)
        self.assertEqual(bounds[0][0], K(2025, 11, 1))
        self.assertEqual(bounds[-1][0], K(2026, 10, 1))

    def test_unknown_period_raises(self):
        with self.assertRaises(ValueError):
            period_bounds('year', 1, K(2026, 10, 9))


class AvailabilityTest(unittest.TestCase):
    def test_unknown_time_is_excluded(self):
        self.assertAlmostEqual(availability_pct(10 * H, 2 * H), 83.333, places=2)

    def test_none_without_known_time(self):
        self.assertIsNone(availability_pct(0, 0))

    def test_window_stats_property(self):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('on', K(2026, 10, 9, 6), now, ongoing=True)]
        ws = window_stats(ivs, K(2026, 10, 9), now)
        self.assertEqual((ws.on, ws.off, ws.unknown), (6 * H, 0, 6 * H))
        self.assertEqual(ws.availability, 100.0)


class OutagesTest(unittest.TestCase):
    def test_newest_first_with_ongoing(self):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('on', K(2026, 10, 9, 0), K(2026, 10, 9, 3)),
               Interval('off', K(2026, 10, 9, 3), K(2026, 10, 9, 5)),
               Interval('on', K(2026, 10, 9, 5), K(2026, 10, 9, 11)),
               Interval('off', K(2026, 10, 9, 11), now, ongoing=True)]
        outs = outages(ivs, K(2026, 10, 9), now)
        self.assertEqual([o.start for o in outs],
                         [K(2026, 10, 9, 11), K(2026, 10, 9, 3)])
        self.assertTrue(outs[0].ongoing)
        self.assertEqual(outs[1].seconds, 2 * H)
        self.assertFalse(outs[1].lower_bound)

    def test_outage_next_to_unknown_is_lower_bound(self):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('unknown', K(2026, 10, 9, 0), K(2026, 10, 9, 2)),
               Interval('off', K(2026, 10, 9, 2), K(2026, 10, 9, 4)),
               Interval('on', K(2026, 10, 9, 4), now, ongoing=True)]
        (out,) = outages(ivs, K(2026, 10, 9), now)
        self.assertTrue(out.start_uncertain)
        self.assertFalse(out.end_uncertain)
        self.assertTrue(out.lower_bound)

    def test_outage_started_before_window_keeps_true_start(self):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('on', K(2026, 10, 7), K(2026, 10, 7, 20)),
               Interval('off', K(2026, 10, 7, 20), now, ongoing=True)]
        start = now - timedelta(hours=24)
        (out,) = outages(ivs, start, now)
        self.assertEqual(out.start, K(2026, 10, 7, 20))
        self.assertEqual(window_stats(ivs, start, now).off, 24 * H)

    def test_summary(self):
        a = Outage(K(2026, 10, 9, 0), K(2026, 10, 9, 2), False, False, False)
        b = Outage(K(2026, 10, 9, 5), K(2026, 10, 9, 6), False, False, False)
        s = outage_summary([a, b])
        self.assertEqual((s.count, s.total, s.average, s.longest),
                         (2, 3 * H, 1.5 * H, 2 * H))
        self.assertEqual(outage_summary([]).count, 0)


class HeatmapTest(unittest.TestCase):
    def test_cells_and_future_hours(self):
        now = K(2026, 10, 9, 12, 30)
        ivs = [Interval('on', K(2026, 10, 8), K(2026, 10, 9, 10)),
               Interval('off', K(2026, 10, 9, 10), now, ongoing=True)]
        rows = hourly_heatmap(ivs, 2, now)
        self.assertEqual([r.day for r in rows],
                         [date(2026, 10, 8), date(2026, 10, 9)])
        today = rows[1]
        self.assertEqual(today.off[9], 0.0)
        self.assertEqual(today.off[10], 1.0)
        self.assertEqual(today.off[12], 1.0)
        self.assertIsNone(today.off[13])
        self.assertEqual(rows[0].unknown[5], 0.0)


if __name__ == '__main__':
    unittest.main()
