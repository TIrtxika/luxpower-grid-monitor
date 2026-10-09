"""
Grid statistics over state intervals (on / off / unknown)
Pure functions: no DB access, Kyiv calendar via zoneinfo
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")
PERIODS = ('day', 'week', 'month')


@dataclass(frozen=True)
class Interval:
    state: str          # 'on' | 'off' | 'unknown'
    start: datetime     # aware
    end: datetime       # aware; for the open interval this is "now"
    ongoing: bool = False


@dataclass(frozen=True)
class PeriodStats:
    start: datetime
    end: datetime
    on: float       # seconds
    off: float
    unknown: float  # includes time not covered by any interval
    partial: bool   # current period, clipped at "now"

    @property
    def availability(self) -> Optional[float]:
        return availability_pct(self.on, self.off)


@dataclass(frozen=True)
class Outage:
    start: datetime
    end: datetime
    ongoing: bool
    start_uncertain: bool  # preceded by unknown time
    end_uncertain: bool    # followed by unknown time

    @property
    def seconds(self) -> float:
        return _seconds(self.start, self.end)

    @property
    def lower_bound(self) -> bool:
        return self.start_uncertain or self.end_uncertain


@dataclass(frozen=True)
class OutageSummary:
    count: int
    total: float
    average: float
    longest: float


@dataclass(frozen=True)
class HeatRow:
    day: date
    off: List[Optional[float]]      # 24 shares 0..1, None = future hour
    unknown: List[Optional[float]]


def availability_pct(on: float, off: float) -> Optional[float]:
    """Share of time with grid among the time we know about"""
    known = on + off
    if known <= 0:
        return None
    return on / known * 100


def _seconds(start: datetime, end: datetime) -> float:
    """Real elapsed seconds. Plain subtraction of datetimes sharing one
    ZoneInfo ignores DST shifts, so go through POSIX timestamps"""
    return end.timestamp() - start.timestamp()


def _totals(intervals: Sequence[Interval], lo: datetime,
            hi: datetime) -> Tuple[float, float, float]:
    """Seconds of on/off/unknown in [lo, hi); uncovered time is unknown"""
    lo_ts, hi_ts = lo.timestamp(), hi.timestamp()
    on = off = unknown = 0.0
    for iv in intervals:
        a = max(iv.start.timestamp(), lo_ts)
        b = min(iv.end.timestamp(), hi_ts)
        if b <= a:
            continue
        seconds = b - a
        if iv.state == 'on':
            on += seconds
        elif iv.state == 'off':
            off += seconds
        else:
            unknown += seconds
    span = max(hi_ts - lo_ts, 0.0)
    unknown += max(span - on - off - unknown, 0.0)
    return on, off, unknown


def window_stats(intervals: Sequence[Interval], start: datetime,
                 end: datetime) -> PeriodStats:
    on, off, unknown = _totals(intervals, start, end)
    return PeriodStats(start, end, on, off, unknown, partial=False)


def _local_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), KYIV_TZ)


def _period_start(d: date, period: str) -> date:
    if period == 'day':
        return d
    if period == 'week':
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def _next_start(d: date, period: str) -> date:
    if period == 'day':
        return d + timedelta(days=1)
    if period == 'week':
        return d + timedelta(days=7)
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _prev_start(d: date, period: str) -> date:
    if period == 'day':
        return d - timedelta(days=1)
    if period == 'week':
        return d - timedelta(days=7)
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


def period_bounds(period: str, count: int,
                  now: datetime) -> List[Tuple[datetime, datetime, bool]]:
    """Last `count` Kyiv periods up to the current one, oldest first"""
    if period not in PERIODS:
        raise ValueError(f"Unknown period: {period}")
    starts = [_period_start(now.astimezone(KYIV_TZ).date(), period)]
    for _ in range(count - 1):
        starts.append(_prev_start(starts[-1], period))
    starts.reverse()

    bounds = []
    for s in starts:
        lo = _local_midnight(s)
        hi = _local_midnight(_next_start(s, period))
        bounds.append((lo, min(hi, now), hi > now))
    return bounds


def split_by_periods(intervals: Sequence[Interval], period: str, count: int,
                     now: datetime) -> List[PeriodStats]:
    result = []
    for lo, hi, partial in period_bounds(period, count, now):
        on, off, unknown = _totals(intervals, lo, hi)
        result.append(PeriodStats(lo, hi, on, off, unknown, partial))
    return result


def outages(intervals: Sequence[Interval], start: datetime,
            end: datetime) -> List[Outage]:
    """Off intervals overlapping [start, end), newest first, true start/end"""
    ivs = sorted(intervals, key=lambda iv: iv.start)
    result = []
    for i, iv in enumerate(ivs):
        if iv.state != 'off' or iv.end <= start or iv.start >= end:
            continue
        prev = ivs[i - 1] if i > 0 else None
        nxt = ivs[i + 1] if i + 1 < len(ivs) else None
        start_uncertain = prev is not None and (
            prev.state == 'unknown' or prev.end < iv.start)
        end_uncertain = (not iv.ongoing and nxt is not None and (
            nxt.state == 'unknown' or nxt.start > iv.end))
        result.append(Outage(iv.start, iv.end, iv.ongoing,
                             start_uncertain, end_uncertain))
    result.sort(key=lambda o: o.start, reverse=True)
    return result


def outage_summary(outs: Sequence[Outage]) -> OutageSummary:
    if not outs:
        return OutageSummary(0, 0.0, 0.0, 0.0)
    seconds = [o.seconds for o in outs]
    return OutageSummary(len(seconds), sum(seconds),
                         sum(seconds) / len(seconds), max(seconds))


def hourly_heatmap(intervals: Sequence[Interval], days: int,
                   now: datetime) -> List[HeatRow]:
    """Day x hour (Kyiv) shares of off and unknown time, oldest day first"""
    today = now.astimezone(KYIV_TZ).date()
    rows = []
    for k in range(days - 1, -1, -1):
        d = today - timedelta(days=k)
        off: List[Optional[float]] = []
        unknown: List[Optional[float]] = []
        for h in range(24):
            lo = datetime.combine(d, time(h), KYIV_TZ)
            hi = (datetime.combine(d, time(h + 1), KYIV_TZ) if h < 23
                  else _local_midnight(d + timedelta(days=1)))
            if lo >= now:
                off.append(None)
                unknown.append(None)
                continue
            hi = min(hi, now)
            span = _seconds(lo, hi)
            if span <= 0:  # skipped hour on the spring DST day
                off.append(0.0)
                unknown.append(0.0)
                continue
            _, off_s, unknown_s = _totals(intervals, lo, hi)
            off.append(off_s / span)
            unknown.append(unknown_s / span)
        rows.append(HeatRow(d, off, unknown))
    return rows
