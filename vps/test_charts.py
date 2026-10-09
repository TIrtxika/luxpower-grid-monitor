"""
Chart service: callback parsing, keyboards, data selection, cache, off-loop render.
Run: python -m unittest test_charts
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import charts
from stats import Interval

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self):
        self.calls = []

    def get_intervals(self, start, end):
        self.calls.append(('intervals', start, end))
        return [Interval('on', NOW - timedelta(days=40), NOW, ongoing=True)]

    def get_status_history(self, hours):
        self.calls.append(('history', hours))
        return [{'timestamp': NOW - timedelta(minutes=5 * i), 'grid_voltage': 230,
                 'battery_soc': 80, 'load_power': 300} for i in range(10, 0, -1)]

    def get_hourly_averages(self, hours):
        self.calls.append(('hourly', hours))
        return [{'hour': NOW - timedelta(hours=i), 'avg_grid_voltage': 230,
                 'avg_battery_soc': 80, 'avg_load_power': 300}
                for i in range(10, 0, -1)]


class ParseTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(charts.parse_callback('ch:p:voltage:7d'),
                         ('p', 'voltage', '7d'))
        self.assertEqual(charts.parse_callback('ch:g:heatmap:30d'),
                         ('g', 'heatmap', '30d'))

    def test_public_cannot_request_inverter_charts(self):
        self.assertIsNone(charts.parse_callback('ch:g:voltage:24h'))

    def test_heatmap_has_no_24h(self):
        self.assertIsNone(charts.parse_callback('ch:p:heatmap:24h'))

    def test_garbage(self):
        for data in ('ch:p', 'ch:x:voltage:24h', 'chart_voltage', 'ch:p:voltage:1y'):
            self.assertIsNone(charts.parse_callback(data))


class KeyboardTest(unittest.TestCase):
    def buttons(self, kb):
        return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]

    def test_public_keyboard_marks_current(self):
        buttons = self.buttons(charts.keyboard('g', 'timeline', '7d'))
        self.assertIn(("• 7 днів", 'ch:g:timeline:7d'), buttons)
        self.assertIn(("24 години", 'ch:g:timeline:24h'), buttons)
        self.assertNotIn('ch:g:voltage:24h', [cb for _, cb in buttons])

    def test_heatmap_periods(self):
        callbacks = [cb for _, cb in self.buttons(charts.keyboard('p', 'heatmap', '7d'))]
        self.assertIn('ch:p:heatmap:30d', callbacks)
        self.assertNotIn('ch:p:heatmap:24h', callbacks)

    def test_type_menu_without_selection(self):
        callbacks = [cb for _, cb in self.buttons(charts.keyboard('p'))]
        self.assertIn('ch:p:voltage:24h', callbacks)
        self.assertIn('ch:p:heatmap:7d', callbacks)


class RenderTest(unittest.TestCase):
    def test_every_chart_renders_png(self):
        for chart_type in charts.CHART_TYPES['p']:
            for period in charts.periods_for(chart_type):
                png = charts.render_chart(chart_type, period, now=NOW, db=FakeDb())
                self.assertTrue(png.startswith(b'\x89PNG'), (chart_type, period))

    def test_month_uses_hourly_averages(self):
        db = FakeDb()
        charts.render_chart('voltage', '30d', now=NOW, db=db)
        self.assertIn(('hourly', 720), db.calls)

    def test_week_uses_raw_samples(self):
        db = FakeDb()
        charts.render_chart('battery', '7d', now=NOW, db=db)
        self.assertIn(('history', 168), db.calls)


class CacheTest(unittest.TestCase):
    def test_cached_for_a_minute(self):
        cache = charts.ChartCache(ttl=60)
        render = MagicMock(return_value=b'png')
        clock = [1000.0]
        with patch.object(charts, 'render_chart', render), \
             patch.object(charts.time, 'monotonic', lambda: clock[0]):
            self.assertEqual(asyncio.run(cache.get('timeline', '24h')), b'png')
            clock[0] += 30
            asyncio.run(cache.get('timeline', '24h'))
            self.assertEqual(render.call_count, 1)
            clock[0] += 31
            asyncio.run(cache.get('timeline', '24h'))
            self.assertEqual(render.call_count, 2)

    def test_renders_off_the_event_loop(self):
        cache = charts.ChartCache(ttl=60)
        with patch.object(charts.asyncio, 'to_thread',
                          new=AsyncMock(return_value=b'png')) as to_thread:
            asyncio.run(cache.get('heatmap', '7d'))
        self.assertIs(to_thread.await_args.args[0], charts.render_chart)


if __name__ == '__main__':
    unittest.main()
