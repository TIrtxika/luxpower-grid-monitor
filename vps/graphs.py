"""
Chart rendering for the Telegram bots
Object-oriented matplotlib (thread-safe, no pyplot), Kyiv time on the axes
"""

import io
import math
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.dates as mdates
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LinearSegmentedColormap, to_hex
from matplotlib.figure import Figure
from matplotlib.patches import Patch, Rectangle

from stats import KYIV_TZ, HeatRow, Interval

DAYS_UA = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']

COLORS = {
    'on': '#2e9d5a',
    'off': '#d64545',
    'unknown': '#b8bcc2',
    'future': '#ffffff',
    'voltage': '#2f6db5',
    'battery': '#2e9d5a',
    'load': '#e08a1e',
    'grid': '#e6e8eb',
    'text': '#2b2f33',
}

PERIOD_LABELS = {'24h': '24 години', '7d': '7 днів', '30d': '30 днів'}

# metric -> (sample field, title, unit, color, reference lines)
METRICS = {
    'voltage': ('grid_voltage', 'Напруга мережі', 'V', COLORS['voltage'],
                [(180, 'поріг')]),
    'battery': ('battery_soc', 'Заряд батареї', '%', COLORS['battery'],
                [(20, 'критично')]),
    'load': ('load_power', 'Навантаження', 'W', COLORS['load'], []),
}

DPI = 160

_HEAT_CMAP = LinearSegmentedColormap.from_list(
    'grid', [COLORS['on'], '#f2c94c', COLORS['off']])


def series_with_gaps(timestamps: Sequence[datetime], values: Sequence,
                     max_gap: timedelta) -> Tuple[list, list]:
    """None -> NaN; a NaN point between samples further apart than max_gap
    so the line breaks instead of bridging missing data"""
    xs, ys = [], []
    prev = None
    for t, v in zip(timestamps, values):
        if prev is not None and t - prev > max_gap:
            xs.append(prev + (t - prev) / 2)
            ys.append(math.nan)
        xs.append(t)
        ys.append(math.nan if v is None else float(v))
        prev = t
    return xs, ys


def state_segments(intervals: Sequence[Interval], start: datetime,
                   end: datetime) -> List[Tuple[str, datetime, datetime]]:
    """Contiguous (state, from, to) covering [start, end); gaps are unknown"""
    segments = []
    cursor = start
    for iv in sorted(intervals, key=lambda i: i.start):
        a, b = max(iv.start, start), min(iv.end, end)
        if b <= a:
            continue
        if a > cursor:
            segments.append(('unknown', cursor, a))
        segments.append((iv.state, a, b))
        cursor = max(cursor, b)
    if cursor < end:
        segments.append(('unknown', cursor, end))
    return segments


def heat_color(off: Optional[float], unknown: Optional[float]) -> str:
    """Heatmap cell: white = future, grey = mostly unknown, green..red by off share"""
    if off is None:
        return COLORS['future']
    if (unknown or 0) >= 0.5:
        return COLORS['unknown']
    if off <= 0:
        return COLORS['on']
    if off >= 1:
        return COLORS['off']
    return to_hex(_HEAT_CMAP(off))


def to_png(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor='white')
    return buf.getvalue()


def _new_figure(width: float, height: float) -> Figure:
    fig = Figure(figsize=(width, height), dpi=DPI, facecolor='white')
    FigureCanvasAgg(fig)
    return fig


def _style_axes(ax):
    ax.set_facecolor('white')
    ax.grid(True, color=COLORS['grid'], linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(COLORS['grid'])
    ax.tick_params(colors=COLORS['text'], labelsize=9)


def _title(ax, text: str):
    ax.set_title(text, loc='left', fontsize=13, color=COLORS['text'],
                 fontweight='bold', pad=10)


def _time_axis(ax, start: datetime, end: datetime):
    ax.set_xlim(start, end)
    span = end - start
    if span <= timedelta(days=1):
        locator = mdates.HourLocator(byhour=range(0, 24, 3), tz=KYIV_TZ)
        fmt = '%H:%M'
    elif span <= timedelta(days=7):
        locator = mdates.DayLocator(tz=KYIV_TZ)
        fmt = '%d.%m'
    else:
        locator = mdates.DayLocator(interval=5, tz=KYIV_TZ)
        fmt = '%d.%m'
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=KYIV_TZ))


def _shade_states(ax, intervals: Sequence[Interval], start: datetime,
                  end: datetime):
    """Red background for outages, hatched grey for unknown time"""
    for state, a, b in state_segments(intervals, start, end):
        if state == 'off':
            ax.axvspan(a, b, color=COLORS['off'], alpha=0.14, linewidth=0)
        elif state == 'unknown':
            ax.axvspan(a, b, facecolor='none', edgecolor=COLORS['unknown'],
                       hatch='///', linewidth=0)


def _empty_note(ax, text: str = "Немає даних за цей період"):
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha='center', va='center',
            color=COLORS['text'], fontsize=12, alpha=0.7)


def _state_legend(ax, loc='upper left'):
    handles = [Patch(color=COLORS['on'], label='є світло'),
               Patch(color=COLORS['off'], label='немає'),
               Patch(facecolor='white', edgecolor=COLORS['unknown'], hatch='///',
                     label='невідомо')]
    ax.legend(handles=handles, loc=loc, fontsize=8, frameon=False, ncol=3)


def _draw_metric(ax, metric: str, samples: Sequence[Dict],
                 intervals: Sequence[Interval], start: datetime, end: datetime,
                 max_gap: timedelta):
    field, _, unit, color, refs = METRICS[metric]
    _style_axes(ax)
    _shade_states(ax, intervals, start, end)
    xs, ys = series_with_gaps([s['timestamp'] for s in samples],
                              [s.get(field) for s in samples], max_gap)
    if any(not math.isnan(y) for y in ys):
        ax.plot(xs, ys, color=color, linewidth=1.6)
    else:
        _empty_note(ax)
    for y, _label in refs:
        ax.axhline(y, color=COLORS['off'], linestyle='--', linewidth=1, alpha=0.6)
    ax.set_ylabel(unit, color=COLORS['text'])
    if metric == 'battery':
        ax.set_ylim(0, 100)


def build_metric_figure(metric: str, samples: Sequence[Dict],
                        intervals: Sequence[Interval], start: datetime,
                        end: datetime, period: str,
                        max_gap: timedelta = timedelta(minutes=10)) -> Figure:
    fig = _new_figure(10, 4.6)
    ax = fig.add_subplot()
    _draw_metric(ax, metric, samples, intervals, start, end, max_gap)
    _title(ax, f"{METRICS[metric][1]} — {PERIOD_LABELS[period]}")
    _time_axis(ax, start, end)
    return fig


def build_combined_figure(samples: Sequence[Dict], intervals: Sequence[Interval],
                          start: datetime, end: datetime, period: str,
                          max_gap: timedelta = timedelta(minutes=10)) -> Figure:
    fig = _new_figure(10, 9)
    axes = fig.subplots(3, 1, sharex=True)
    for ax, metric in zip(axes, ('voltage', 'battery', 'load')):
        _draw_metric(ax, metric, samples, intervals, start, end, max_gap)
        ax.set_ylabel(f"{METRICS[metric][1]}, {METRICS[metric][2]}",
                      color=COLORS['text'], fontsize=9)
    _title(axes[0], f"Інвертор — {PERIOD_LABELS[period]}")
    _time_axis(axes[-1], start, end)
    return fig


def _local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=KYIV_TZ)


def _hours_since(t: datetime, origin: datetime) -> float:
    return (t.timestamp() - origin.timestamp()) / 3600


def build_timeline_figure(intervals: Sequence[Interval], start: datetime,
                          end: datetime, period: str) -> Figure:
    """24h: one band on a time axis. 7d/30d: one row per Kyiv day, 0..24 h"""
    if period == '24h':
        fig = _new_figure(10, 2.4)
        ax = fig.add_subplot()
        _style_axes(ax)
        for state, a, b in state_segments(intervals, start, end):
            x0 = mdates.date2num(a)
            ax.broken_barh([(x0, mdates.date2num(b) - x0)], (0, 1),
                           **_bar_style(state))
        ax.set_ylim(0, 1)
        ax.set_yticks([])
        _time_axis(ax, start, end)
        _title(ax, f"Світло — {PERIOD_LABELS[period]}")
        _state_legend(ax, loc='upper left')
        ax.legend_.set_bbox_to_anchor((0, -0.35))
        return fig

    first = start.astimezone(KYIV_TZ).date()
    last = end.astimezone(KYIV_TZ).date()
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    fig = _new_figure(10, 0.32 * len(days) + 1.6)
    ax = fig.add_subplot()
    _style_axes(ax)
    for row, day in enumerate(days):
        lo = _local_midnight(day)
        hi = min(_local_midnight(day + timedelta(days=1)), end)
        if hi <= lo:
            continue
        for state, a, b in state_segments(intervals, lo, hi):
            ax.broken_barh([(_hours_since(a, lo), _hours_since(b, a))],
                           (row + 0.12, 0.76), **_bar_style(state))
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 3))
    ax.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 3)])
    ax.set_ylim(len(days), 0)
    ax.set_yticks([i + 0.5 for i in range(len(days))])
    ax.set_yticklabels([f"{DAYS_UA[d.weekday()]} {d:%d.%m}" for d in days])
    ax.grid(False)
    _title(ax, f"Світло — {PERIOD_LABELS[period]}")
    _state_legend(ax, loc='lower left')
    ax.legend_.set_bbox_to_anchor((0, 1.0))
    return fig


def _bar_style(state: str) -> Dict:
    if state == 'unknown':
        return {'facecolor': 'white', 'edgecolor': COLORS['unknown'],
                'hatch': '///', 'linewidth': 0}
    return {'facecolor': COLORS[state], 'linewidth': 0}


def build_heatmap_figure(rows: Sequence[HeatRow], period: str) -> Figure:
    """Day x hour: share of time without grid (grey = mostly unknown)"""
    fig = _new_figure(10, 0.32 * len(rows) + 1.8)
    ax = fig.add_subplot()
    ax.set_facecolor('white')
    for r, row in enumerate(rows):
        for h in range(24):
            ax.add_patch(Rectangle((h, r), 1, 1,
                                   facecolor=heat_color(row.off[h], row.unknown[h]),
                                   edgecolor='white', linewidth=1))
    ax.set_xlim(0, 24)
    ax.set_ylim(len(rows), 0)
    ax.set_xticks(range(0, 25, 3))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 25, 3)])
    ax.set_yticks([i + 0.5 for i in range(len(rows))])
    ax.set_yticklabels([f"{DAYS_UA[r.day.weekday()]} {r.day:%d.%m}" for r in rows])
    ax.tick_params(colors=COLORS['text'], labelsize=9, length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    _title(ax, f"Відключення по годинах — {PERIOD_LABELS[period]}")
    handles = [Patch(color=COLORS['on'], label='світло було'),
               Patch(color='#f2c94c', label='частково'),
               Patch(color=COLORS['off'], label='не було'),
               Patch(color=COLORS['unknown'], label='невідомо')]
    ax.legend(handles=handles, loc='lower left', bbox_to_anchor=(0, 1.0),
              fontsize=8, frameon=False, ncol=4)
    return fig
