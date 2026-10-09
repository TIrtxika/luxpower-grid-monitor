"""
Chart renderers: gaps, Kyiv axes, state shading, timeline, heatmap.
Run: python -m unittest test_graphs
"""

import math
import unittest
from datetime import datetime, timedelta, timezone

import graphs
from stats import KYIV_TZ, HeatRow, Interval

UTC = timezone.utc


def K(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=KYIV_TZ)


def samples(start, count, step=timedelta(minutes=5), **fields):
    return [dict({'timestamp': start + i * step}, **fields) for i in range(count)]


class SeriesTest(unittest.TestCase):
    def test_none_becomes_nan(self):
        t0 = datetime(2026, 10, 9, tzinfo=UTC)
        xs, ys = graphs.series_with_gaps([t0, t0 + timedelta(minutes=5)], [230, None],
                                         timedelta(minutes=10))
        self.assertEqual(ys[0], 230.0)
        self.assertTrue(math.isnan(ys[1]))

    def test_gap_inserts_break(self):
        t0 = datetime(2026, 10, 9, tzinfo=UTC)
        xs, ys = graphs.series_with_gaps([t0, t0 + timedelta(hours=1)], [230, 231],
                                         timedelta(minutes=10))
        self.assertEqual(len(xs), 3)
        self.assertTrue(math.isnan(ys[1]))


class SegmentsTest(unittest.TestCase):
    def test_uncovered_time_is_unknown(self):
        ivs = [Interval('on', K(2026, 10, 9, 0), K(2026, 10, 9, 6)),
               Interval('off', K(2026, 10, 9, 8), K(2026, 10, 9, 10))]
        segs = graphs.state_segments(ivs, K(2026, 10, 9), K(2026, 10, 9, 12))
        self.assertEqual([s[0] for s in segs], ['on', 'unknown', 'off', 'unknown'])
        self.assertEqual(segs[1][1:], (K(2026, 10, 9, 6), K(2026, 10, 9, 8)))


class MetricFigureTest(unittest.TestCase):
    def test_axis_in_kyiv_time_and_png(self):
        now = datetime(2026, 10, 9, 12, tzinfo=UTC)
        start = now - timedelta(hours=24)
        data = samples(start, 288, grid_voltage=230)
        fig = graphs.build_metric_figure('voltage', data, [], start, now, '24h')
        ax = fig.axes[0]
        self.assertIs(ax.xaxis.get_major_formatter().tz, KYIV_TZ)
        self.assertEqual(len(ax.lines) >= 1, True)
        self.assertTrue(graphs.to_png(fig).startswith(b'\x89PNG'))

    def test_empty_data_still_renders(self):
        now = datetime(2026, 10, 9, 12, tzinfo=UTC)
        fig = graphs.build_metric_figure('battery', [], [], now - timedelta(days=7),
                                         now, '7d')
        self.assertTrue(graphs.to_png(fig).startswith(b'\x89PNG'))

    def test_outage_is_shaded(self):
        now = datetime(2026, 10, 9, 12, tzinfo=UTC)
        start = now - timedelta(hours=24)
        ivs = [Interval('off', now - timedelta(hours=3), now - timedelta(hours=1))]
        fig = graphs.build_metric_figure('load', samples(start, 10, load_power=300),
                                         ivs, start, now, '24h')
        self.assertGreaterEqual(len(fig.axes[0].patches), 1)

    def test_combined_has_three_panels(self):
        now = datetime(2026, 10, 9, 12, tzinfo=UTC)
        start = now - timedelta(hours=24)
        data = samples(start, 20, grid_voltage=230, battery_soc=80, load_power=300)
        fig = graphs.build_combined_figure(data, [], start, now, '24h')
        self.assertEqual(len(fig.axes), 3)


class TimelineTest(unittest.TestCase):
    def test_week_has_a_row_per_day(self):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('on', K(2026, 10, 1), now, ongoing=True)]
        fig = graphs.build_timeline_figure(ivs, K(2026, 10, 3), now, '7d')
        labels = [t.get_text() for t in fig.axes[0].get_yticklabels()]
        self.assertEqual(len(labels), 7)
        self.assertEqual(labels[0], "Сб 03.10")
        self.assertTrue(graphs.to_png(fig).startswith(b'\x89PNG'))

    def test_day_band_renders(self):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('off', K(2026, 10, 9, 1), K(2026, 10, 9, 3))]
        fig = graphs.build_timeline_figure(ivs, now - timedelta(hours=24), now, '24h')
        self.assertTrue(graphs.to_png(fig).startswith(b'\x89PNG'))


class HeatmapTest(unittest.TestCase):
    def test_cell_colors(self):
        self.assertEqual(graphs.heat_color(None, None), graphs.COLORS['future'])
        self.assertEqual(graphs.heat_color(0.0, 0.8), graphs.COLORS['unknown'])
        self.assertEqual(graphs.heat_color(0.0, 0.0), graphs.COLORS['on'])
        self.assertEqual(graphs.heat_color(1.0, 0.0), graphs.COLORS['off'])

    def test_figure_rows(self):
        rows = [HeatRow(K(2026, 10, 8).date(), [0.0] * 24, [0.0] * 24),
                HeatRow(K(2026, 10, 9).date(), [1.0] * 12 + [None] * 12,
                        [0.0] * 12 + [None] * 12)]
        fig = graphs.build_heatmap_figure(rows, '7d')
        labels = [t.get_text() for t in fig.axes[0].get_yticklabels()]
        self.assertEqual(labels, ["Чт 08.10", "Пт 09.10"])
        self.assertTrue(graphs.to_png(fig).startswith(b'\x89PNG'))


if __name__ == '__main__':
    unittest.main()
