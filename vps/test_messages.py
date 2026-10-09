"""
Message formatting for statistics.
Run: python -m unittest test_messages
"""

import unittest
from datetime import datetime

from messages import (
    BAR_OFF, BAR_ON, BAR_UNKNOWN, format_duration, format_outages,
    format_periods, format_since, make_bar,
)
from stats import KYIV_TZ, Outage, PeriodStats

H = 3600


def K(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=KYIV_TZ)


class MessagesTest(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(format_duration(45), "45 сек")
        self.assertEqual(format_duration(2 * H + 300), "2 год 5 хв")
        self.assertEqual(format_duration(8 * 24 * H), "8 дн.")

    def test_bar(self):
        self.assertEqual(make_bar(12 * H, 12 * H, 0), BAR_ON * 8 + BAR_OFF * 8)
        self.assertEqual(make_bar(0, 0, 0), BAR_UNKNOWN * 16)
        self.assertEqual(make_bar(8 * H, 0, 16 * H), BAR_ON * 5 + BAR_UNKNOWN * 11)

    def test_periods_show_unknown_and_exclude_it_from_availability(self):
        days = [PeriodStats(K(2026, 10, 8), K(2026, 10, 9), 0, 0, 24 * H, False),
                PeriodStats(K(2026, 10, 9), K(2026, 10, 9, 12), 10 * H, 2 * H, 0, True)]
        text = format_periods(days, 'day')
        self.assertIn("за тиждень", text)
        self.assertIn("Чт 08.10", text)
        self.assertIn("Пт 09.10*", text)
        self.assertIn("Доступність: 83.3%", text)
        self.assertIn("Немає даних: 24 год", text)
        self.assertIn("* — неповний період", text)

    def test_outages_list(self):
        ongoing = Outage(K(2026, 10, 9, 11), K(2026, 10, 9, 12), True, False, False)
        bounded = Outage(K(2026, 10, 9, 2), K(2026, 10, 9, 4), False, True, False)
        text = format_outages([ongoing, bounded])
        self.assertIn("з 11:00 09.10 — триває (1 год)", text)
        self.assertIn("02:00 09.10 – 04:00 (≥ 2 год)", text)

    def test_no_outages(self):
        self.assertIn("не було", format_outages([]))

    def test_since(self):
        self.assertEqual(format_since(K(2026, 10, 9, 10), K(2026, 10, 9, 12)),
                         "З 10:00 (2 год)")
        self.assertEqual(format_since(K(2026, 10, 8, 23), K(2026, 10, 9, 1)),
                         "З 23:00 08.10 (2 год)")

    def test_since_across_dst_change(self):
        # 2026-10-25 04:00 EEST -> 03:00 EET: 00:00 -> 06:00 is 7 real hours
        self.assertEqual(format_since(K(2026, 10, 25, 0), K(2026, 10, 25, 6)),
                         "З 00:00 (7 год)")


if __name__ == '__main__':
    unittest.main()
