"""
Chart renderers: gaps, Kyiv axes, state shading, timeline, heatmap.
Run: python -m unittest test_graphs
"""

import math
import unittest
from datetime import datetime, timedelta, timezone

import graphs
from schedule import PlannedOutage
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


def plan_bars(fig):
    """(x, width) of planned-outage bars, by row y"""
    out = []
    for c in fig.axes[0].collections:
        if c.get_gid() != 'plan':
            continue
        for path in c.get_paths():
            xs = [v[0] for v in path.vertices]
            ys = [v[1] for v in path.vertices]
            out.append((round(min(xs), 2), round(max(xs) - min(xs), 2), round(min(ys), 2)))
    return sorted(out)


class PlanLaneTest(unittest.TestCase):
    PLAN = [PlannedOutage(K(2026, 10, 9, 9), K(2026, 10, 9, 12, 30)),
            PlannedOutage(K(2026, 10, 9, 18), K(2026, 10, 9, 22))]

    def week(self, planned):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('on', K(2026, 10, 1), now, ongoing=True)]
        return graphs.build_timeline_figure(ivs, K(2026, 10, 3), now, '7d',
                                            planned=planned)

    def test_day_rows_show_the_whole_day_plan_including_the_future(self):
        bars = plan_bars(self.week(self.PLAN))
        # today is the 7th row (index 6); 18:00-22:00 is still ahead
        self.assertEqual([(x, w) for x, w, _ in bars], [(9.0, 3.5), (18.0, 4.0)])
        self.assertTrue(all(6 < y < 7 for _, _, y in bars))

    def test_plan_legend_entry(self):
        fig = self.week(self.PLAN)
        labels = [t.get_text() for t in fig.axes[0].get_legend().get_texts()]
        self.assertIn('за графіком', labels)

    def test_no_plan_lane_without_schedule(self):
        fig = self.week(None)
        self.assertEqual(plan_bars(fig), [])
        labels = [t.get_text() for t in fig.axes[0].get_legend().get_texts()]
        self.assertNotIn('за графіком', labels)

    def test_day_band_plan_clipped_to_window(self):
        now = K(2026, 10, 9, 12)
        fig = graphs.build_timeline_figure([], now - timedelta(hours=24), now, '24h',
                                           planned=self.PLAN)
        (bar,) = plan_bars(fig)
        x0, width, _ = bar
        nine = graphs.mdates.date2num(K(2026, 10, 9, 9))
        self.assertAlmostEqual(x0, round(nine, 2))
        self.assertAlmostEqual(width, round(3 / 24, 2))  # 09:00-12:00, until now
        self.assertTrue(graphs.to_png(fig).startswith(b'\x89PNG'))


class LayoutTest(unittest.TestCase):
    def assert_legend_clear_of_title(self, fig):
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        ax = fig.axes[0]
        # set_title(loc='left') draws into the left title artist, not ax.title
        title = ax._left_title.get_window_extent(renderer)
        legend = ax.get_legend().get_window_extent(renderer)
        self.assertFalse(title.overlaps(legend), (title, legend))

    def test_timeline_week_legend_does_not_cover_title(self):
        now = K(2026, 10, 9, 12)
        ivs = [Interval('on', K(2026, 10, 1), now, ongoing=True)]
        self.assert_legend_clear_of_title(
            graphs.build_timeline_figure(ivs, K(2026, 10, 3), now, '7d'))

    def test_legend_with_plan_does_not_cover_title(self):
        now = K(2026, 10, 9, 12)
        plan = PlanLaneTest.PLAN
        self.assert_legend_clear_of_title(graphs.build_timeline_figure(
            [], K(2026, 10, 3), now, '7d', planned=plan))
        self.assert_legend_clear_of_title(graphs.build_timeline_figure(
            [], now - timedelta(hours=24), now, '24h', planned=plan))

    def test_timeline_day_legend_does_not_cover_title(self):
        now = K(2026, 10, 9, 12)
        self.assert_legend_clear_of_title(
            graphs.build_timeline_figure([], now - timedelta(hours=24), now, '24h'))

    def test_heatmap_legend_does_not_cover_title(self):
        rows = [HeatRow(K(2026, 10, 9).date(), [0.0] * 24, [0.0] * 24)]
        self.assert_legend_clear_of_title(graphs.build_heatmap_figure(rows, '30d'))


if __name__ == '__main__':
    unittest.main()
