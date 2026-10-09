"""
Battery runtime forecast and low-battery thresholds.
Run: python -m unittest test_battery
"""

import unittest

from battery import LowBatteryWatch, forecast

MIN = 60


def falling(start_soc, per_min, minutes, t0=0):
    return [(t0 + m * MIN, start_soc - per_min * m) for m in range(minutes + 1)]


class ForecastTest(unittest.TestCase):
    def test_linear_drop(self):
        # 0.5 %/min = 30 %/h; from 50 % to empty 10 % -> 80 min
        samples = falling(60, 0.5, 20)
        fc = forecast(samples, now=20 * MIN, empty_soc=10)
        self.assertEqual(fc.soc, 50)
        self.assertAlmostEqual(fc.rate_per_hour, 30, places=6)
        self.assertAlmostEqual(fc.seconds_left, 80 * MIN, places=3)

    def test_uses_only_last_30_minutes(self):
        # fast drop an hour ago, slow drop now: forecast follows the recent rate
        old = falling(90, 2, 30, t0=0)
        recent = falling(30, 0.1, 30, t0=31 * MIN)
        fc = forecast(old + recent, now=61 * MIN, empty_soc=10)
        self.assertAlmostEqual(fc.rate_per_hour, 6, places=6)

    def test_too_little_data(self):
        self.assertIsNone(forecast(falling(50, 1, 5), now=5 * MIN, empty_soc=10))
        self.assertIsNone(forecast([(0, 50)], now=0, empty_soc=10))

    def test_flat_or_charging_has_no_forecast(self):
        flat = [(m * MIN, 50) for m in range(20)]
        self.assertIsNone(forecast(flat, now=19 * MIN, empty_soc=10))
        rising = [(m * MIN, 40 + m) for m in range(20)]
        self.assertIsNone(forecast(rising, now=19 * MIN, empty_soc=10))

    def test_already_below_empty_level(self):
        fc = forecast(falling(15, 0.5, 20), now=20 * MIN, empty_soc=10)
        self.assertEqual(fc.seconds_left, 0)

    def test_missing_soc_values_are_skipped(self):
        samples = falling(60, 0.5, 20)
        samples.insert(5, (5.5 * MIN, None))
        self.assertIsNotNone(forecast(samples, now=20 * MIN, empty_soc=10))


class LowBatteryWatchTest(unittest.TestCase):
    def test_each_threshold_fires_once(self):
        w = LowBatteryWatch((30, 15))
        self.assertIsNone(w.observe(0, 40))
        self.assertEqual(w.observe(60, 30), 30)
        self.assertIsNone(w.observe(120, 29))
        self.assertEqual(w.observe(180, 15), 15)
        self.assertIsNone(w.observe(240, 14))

    def test_big_drop_reports_lowest_threshold_only(self):
        w = LowBatteryWatch((30, 15))
        self.assertEqual(w.observe(0, 12), 15)
        self.assertIsNone(w.observe(60, 11))

    def test_reset_rearms_thresholds_and_clears_samples(self):
        w = LowBatteryWatch((30, 15))
        w.observe(0, 25)
        w.reset()
        self.assertEqual(w.samples, [])
        self.assertEqual(w.observe(60, 28), 30)

    def test_keeps_recent_samples_only(self):
        w = LowBatteryWatch((30, 15))
        for m in range(120):
            w.observe(m * MIN, 90)
        self.assertLessEqual(w.samples[-1][0] - w.samples[0][0], 60 * MIN)


if __name__ == '__main__':
    unittest.main()
