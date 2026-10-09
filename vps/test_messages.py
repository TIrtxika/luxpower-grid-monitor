"""
Message formatting for statistics.
Run: python -m unittest test_messages
"""

import unittest
from datetime import datetime

from messages import (
    BAR_OFF, BAR_ON, BAR_UNKNOWN, format_duration, format_inverter_details,
    format_outages, format_periods, format_since, format_traffic_light,
    make_bar, status_from_sample,
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


class TrafficLightTest(unittest.TestCase):
    NOW = K(2026, 10, 9, 12)

    def test_on(self):
        text = format_traffic_light('on', None, K(2026, 10, 9, 10), self.NOW,
                                    voltage=231)
        self.assertEqual(text.split("\n"),
                         ["\U0001f7e2 Світло є", "З 10:00 (2 год)", "Напруга: 231V"])

    def test_off(self):
        text = format_traffic_light('off', None, K(2026, 10, 9, 11), self.NOW,
                                    voltage=0)
        self.assertTrue(text.startswith("\U0001f534 Світла немає\nЗ 11:00 (1 год)"))

    def test_unknown_shows_reason_and_data_age_without_voltage(self):
        text = format_traffic_light('unknown', "дані застарілі",
                                    K(2026, 10, 9, 11, 30), self.NOW,
                                    voltage=230, data_age_s=600)
        self.assertEqual(text.split("\n"),
                         ["\U0001f7e1 Невідомо — дані застарілі",
                          "З 11:30 (30 хв)", "Останні дані: 10 хв тому"])

    def test_without_since(self):
        self.assertEqual(format_traffic_light('on', None, None, self.NOW),
                         "\U0001f7e2 Світло є")


class InverterDetailsTest(unittest.TestCase):
    def test_details(self):
        text = format_inverter_details({
            'grid': {'voltage': 230, 'frequency': 50},
            'battery': {'soc': 80, 'voltage': 52.1, 'current': -3, 'power': -150},
            'output': {'load_power': 420, 'voltage': 230, 'frequency': 50},
            'temperature': {'inverter': 40, 'radiator': 35.5},
            'dc_bus_voltage': 380,
        })
        self.assertIn("Мережа: 230V / 50Hz", text)
        self.assertIn("Батарея: 80%", text)
        self.assertIn("Навантаження: 420W", text)
        self.assertIn("DC Bus: 380V", text)

    def test_sample_to_status(self):
        status = status_from_sample({
            'grid_available': True, 'grid_voltage': 229.5, 'grid_frequency': 50.0,
            'battery_soc': 77, 'battery_voltage': 52.0, 'battery_current': 1.5,
            'battery_power': 78.0, 'load_power': 300, 'output_voltage': 230.0,
            'output_frequency': 50.0, 'inverter_temp': 41, 'radiator_temp': 36.0,
            'dc_bus_voltage': 380.0,
        })
        self.assertEqual(status['grid'], {'available': True, 'voltage': 229.5,
                                          'frequency': 50.0})
        self.assertEqual(status['battery']['soc'], 77)
        self.assertEqual(status['output']['load_power'], 300)
        self.assertEqual(status['temperature'], {'inverter': 41, 'radiator': 36.0})


if __name__ == '__main__':
    unittest.main()
