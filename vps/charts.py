"""
Chart requests: pick data per chart and period, render off the event loop,
cache briefly, build the inline keyboards

Callback data: "ch:<scope>:<type>:<period>", scope p = private bot, g = public
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
import graphs
from database import get_db
from stats import hourly_heatmap, period_bounds

CHART_TYPES = {
    'p': ['voltage', 'battery', 'load', 'combined', 'timeline', 'heatmap'],
    'g': ['timeline', 'heatmap'],
}

TYPE_LABELS = {
    'voltage': "Напруга",
    'battery': "Батарея",
    'load': "Навантаження",
    'combined': "Все разом",
    'timeline': "Шкала світла",
    'heatmap': "Теплова карта",
}

PERIOD_SPAN = {'24h': timedelta(hours=24), '7d': timedelta(days=7),
               '30d': timedelta(days=30)}
PERIOD_DAYS = {'24h': 1, '7d': 7, '30d': 30}

CACHE_SECONDS = 60


def periods_for(chart_type: str) -> List[str]:
    if chart_type == 'heatmap':
        return ['7d', '30d']
    return ['24h', '7d', '30d']


def parse_callback(data: str) -> Optional[Tuple[str, str, str]]:
    """'ch:p:voltage:24h' -> ('p', 'voltage', '24h'); None if not allowed"""
    parts = (data or '').split(':')
    if len(parts) != 4 or parts[0] != 'ch':
        return None
    _, scope, chart_type, period = parts
    if scope not in CHART_TYPES or chart_type not in CHART_TYPES[scope]:
        return None
    if period not in periods_for(chart_type):
        return None
    return scope, chart_type, period


def caption(chart_type: str, period: str) -> str:
    return f"{TYPE_LABELS[chart_type]} — {graphs.PERIOD_LABELS[period]}"


def keyboard(scope: str, chart_type: Optional[str] = None,
             period: Optional[str] = None) -> InlineKeyboardMarkup:
    """Chart type buttons, plus period buttons for the selected chart"""
    def cb(t: str, p: str) -> str:
        return f"ch:{scope}:{t}:{p}"

    type_buttons = []
    for t in CHART_TYPES[scope]:
        p = period if period in periods_for(t) else periods_for(t)[0]
        label = f"• {TYPE_LABELS[t]}" if t == chart_type else TYPE_LABELS[t]
        type_buttons.append(InlineKeyboardButton(label, callback_data=cb(t, p)))
    rows = [type_buttons[i:i + 2] for i in range(0, len(type_buttons), 2)]

    if chart_type:
        rows.append([
            InlineKeyboardButton(
                f"• {graphs.PERIOD_LABELS[p]}" if p == period
                else graphs.PERIOD_LABELS[p],
                callback_data=cb(chart_type, p))
            for p in periods_for(chart_type)
        ])
    return InlineKeyboardMarkup(rows)


def _hourly_sample(row: Dict) -> Dict:
    return {'timestamp': row['hour'],
            'grid_voltage': row.get('avg_grid_voltage'),
            'battery_soc': row.get('avg_battery_soc'),
            'load_power': row.get('avg_load_power')}


def render_chart(chart_type: str, period: str, now: datetime = None,
                 db=None) -> bytes:
    """Fetch the data a chart needs and render it to PNG (blocking)"""
    now = now or datetime.now(timezone.utc)
    db = db or get_db()

    if chart_type == 'heatmap':
        days = PERIOD_DAYS[period]
        first = period_bounds('day', days, now)[0][0]
        rows = hourly_heatmap(db.get_intervals(first, now), days, now)
        return graphs.to_png(graphs.build_heatmap_figure(rows, period))

    if chart_type == 'timeline':
        if period == '24h':
            start = now - PERIOD_SPAN[period]
        else:
            start = period_bounds('day', PERIOD_DAYS[period], now)[0][0]
        fig = graphs.build_timeline_figure(db.get_intervals(start, now),
                                           start, now, period)
        return graphs.to_png(fig)

    start = now - PERIOD_SPAN[period]
    intervals = db.get_intervals(start, now)
    hours = int(PERIOD_SPAN[period].total_seconds() // 3600)
    if period == '30d':
        samples = [_hourly_sample(r) for r in db.get_hourly_averages(hours)]
        max_gap = timedelta(hours=2)
    else:
        samples = db.get_status_history(hours)
        max_gap = timedelta(seconds=2 * config.STORE_INTERVAL)

    if chart_type == 'combined':
        fig = graphs.build_combined_figure(samples, intervals, start, now,
                                           period, max_gap)
    else:
        fig = graphs.build_metric_figure(chart_type, samples, intervals, start,
                                         now, period, max_gap)
    return graphs.to_png(fig)


class ChartCache:
    """Rendered PNGs per (type, period) for a short time"""

    def __init__(self, ttl: float = CACHE_SECONDS):
        self.ttl = ttl
        self._items: Dict[Tuple[str, str], Tuple[float, bytes]] = {}

    async def get(self, chart_type: str, period: str) -> bytes:
        key = (chart_type, period)
        now = time.monotonic()
        hit = self._items.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        png = await asyncio.to_thread(render_chart, chart_type, period)
        self._items[key] = (now, png)
        return png
