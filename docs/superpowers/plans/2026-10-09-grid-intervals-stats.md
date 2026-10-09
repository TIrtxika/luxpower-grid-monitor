# Grid Intervals & Real Statistics (Stage 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the grid state as on/off/unknown intervals and compute honest statistics (Kyiv days/weeks/months, outage list incl. the ongoing one, hour-of-day data) from them, plus the alert/logging fixes listed in the spec.

**Architecture:** A pure `StateTracker` (grid_state.py) turns each RPi poll into an effective state; the poller records state changes as rows in a new `grid_intervals` table (heartbeat + startup gap → unknown). A pure `stats.py` computes all numbers from interval lists; `messages.py` formats them; `bot.py` handlers only fetch intervals and format. Alerts move to a `GridChange` / unknown-state callback model.

**Tech Stack:** Python 3.11 (Debian 12 on the VPS; code must stay 3.9+ compatible), python-telegram-bot ≥ 20, psycopg2, PostgreSQL 15, `unittest`, `zoneinfo`.

**Spec:** `docs/superpowers/specs/2026-10-09-grid-intervals-stats-design.md`

## Global Constraints

- No new dependencies (only what `vps/requirements.txt` already lists).
- All user-facing text in Ukrainian; all displayed times in `Europe/Kyiv`.
- New config defaults: `STALE_DATA_SECONDS = 180`, `UNKNOWN_ALERT_AFTER = 300`, `RETENTION_DAYS = 90`.
- Interval boundaries use the VPS clock; DB columns are `TIMESTAMPTZ`; Python datetimes are timezone-aware.
- Migration: a gap > 10 minutes between `inverter_status` samples becomes `unknown`.
- `events` and `inverter_status` tables are kept; intervals and events are never deleted by retention.
- Do not change anything under `rpi/`.
- Tests are `unittest` files `vps/test_*.py`, run from `vps/`. `$PY` = a Python with `vps/requirements.txt` installed (in this session: `/Users/v.zhuk/.claude/jobs/2e139592/tmp/venv/bin/python`). Full suite: `$PY -m unittest discover -s . -p 'test_*.py'`.
- Commit messages end with the session's attribution lines.

## Review Focus

1. Bot restarted quickly (< 3 polls) with an open interval → no "unknown" interval, no alert, just a heartbeat. Test in Task 5 (`test_quick_restart_only_heartbeats`).
2. A state boundary earlier than the open interval's start → no negative/overlapping intervals (boundary clamped). Test in Task 3 (`test_boundary_before_open_start_is_clamped`).
3. Empty DB (fresh install, no intervals) → `/grid` shows all-unknown bars, `/history` says no outages, `/status` works without a "З ..." line. Tests in Task 7 (`test_grid_on_empty_db_is_all_unknown`, `test_history_on_empty_db`).
4. Outage longer than the window that started before it → list shows the true start, window totals are clipped. Test in Task 2 (`test_outage_started_before_window_keeps_true_start`).
5. Running the migration twice → refused without `--force`. Test in Task 4 (`test_main_refuses_when_intervals_exist`).

## File Structure

| File | Responsibility |
|------|----------------|
| `vps/grid_state.py` (new) | State constants, `Transition`, `GridChange`, pure `StateTracker` |
| `vps/stats.py` (new) | `Interval`, `PeriodStats`, `Outage`, `OutageSummary`, `HeatRow`; pure statistics |
| `vps/messages.py` (new) | Text formatting for stats/outages/durations |
| `vps/migrate_intervals.py` (new) | One-off backfill from `inverter_status` |
| `vps/config.py` | New settings |
| `vps/database.py` | `grid_intervals` table + methods; old stats methods removed (Task 7) |
| `vps/poller.py` | Uses `StateTracker`, records intervals, unknown alerts, retention |
| `vps/alerts.py` | `GridChange`/unknown callbacks, save-first, blocked users, thread-safe dispatch |
| `vps/bot.py` | Handlers use intervals; httpx logging; `post_init` wiring |
| `README.md` | Document new settings and migration |

---

### Task 1: Effective state tracker

**Files:**
- Create: `vps/grid_state.py`
- Modify: `vps/config.py` (add settings after line 49)
- Test: `vps/test_grid_state.py`

**Interfaces:**
- Produces: constants `ON='on'`, `OFF='off'`, `UNKNOWN='unknown'`, `REASON_RPI_UNREACHABLE`, `REASON_DONGLE_OFFLINE`, `REASON_STALE_DATA`, `REASON_NO_GRID_DATA`, `REASON_MONITOR_DOWNTIME`; `Transition(state, at: float, previous, previous_known, reason=None)`; `GridChange(state, at: float, duration_s: Optional[int], approximate: bool)`; `StateTracker(debounce_s, unreachable_threshold, stale_after_s, initial_state=None, initial_since=0.0, last_known=None, initial_reason=None)` with attributes `state`, `since`, `reason`, `last_known` and method `observe(status: Optional[dict], now: float) -> Optional[Transition]`.
- Produces: `config.STALE_DATA_SECONDS`, `config.UNKNOWN_ALERT_AFTER`, `config.RETENTION_DAYS` (ints).

- [ ] **Step 1: Add config settings**

In `vps/config.py`, after `RPI_UNREACHABLE_THRESHOLD = 3` (line 49) add:

```python

# Дані RPi старші за це (секунди) вважаються застарілими -> стан "невідомо"
STALE_DATA_SECONDS = int(os.environ.get("STALE_DATA_SECONDS", "180"))

# Через скільки секунд стану "невідомо" надіслати алерт власнику
UNKNOWN_ALERT_AFTER = int(os.environ.get("UNKNOWN_ALERT_AFTER", "300"))

# Скільки днів зберігати зразки inverter_status
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
```

- [ ] **Step 2: Write the failing tests**

Create `vps/test_grid_state.py`:

```python
"""
StateTracker tests: on/off/unknown from RPi polls.
Run: python -m unittest test_grid_state
"""

import unittest

from grid_state import (
    StateTracker, ON, OFF, UNKNOWN,
    REASON_RPI_UNREACHABLE, REASON_DONGLE_OFFLINE, REASON_STALE_DATA,
    REASON_NO_GRID_DATA,
)


def st(available=True, connected=True, age=5):
    return {'connected': connected, 'data_age_seconds': age,
            'grid': {'available': available, 'voltage': 230}}


def tracker(**seed):
    return StateTracker(debounce_s=120, unreachable_threshold=3,
                        stale_after_s=180, **seed)


class StateTrackerTest(unittest.TestCase):
    def test_first_observation_sets_state(self):
        t = tracker()
        tr = t.observe(st(True), 0)
        self.assertEqual((tr.state, tr.at, tr.previous, tr.previous_known),
                         (ON, 0, None, None))
        self.assertEqual(t.last_known, ON)

    def test_change_confirmed_after_debounce_at_first_seen(self):
        t = tracker(initial_state=ON, last_known=ON)
        self.assertIsNone(t.observe(st(False), 100))
        self.assertIsNone(t.observe(st(False), 160))
        tr = t.observe(st(False), 220)
        self.assertEqual((tr.state, tr.at, tr.previous, tr.previous_known),
                         (OFF, 100, ON, ON))

    def test_flap_cancels_pending_change(self):
        t = tracker(initial_state=ON, last_known=ON)
        t.observe(st(False), 100)
        self.assertIsNone(t.observe(st(True), 160))
        self.assertIsNone(t.observe(st(False), 220))
        self.assertIsNone(t.observe(st(False), 280))
        self.assertEqual(t.state, ON)

    def test_unreachable_after_threshold_starts_at_first_failure(self):
        t = tracker(initial_state=ON, last_known=ON)
        self.assertIsNone(t.observe(None, 100))
        self.assertIsNone(t.observe(None, 160))
        tr = t.observe(None, 220)
        self.assertEqual((tr.state, tr.at, tr.reason),
                         (UNKNOWN, 100, REASON_RPI_UNREACHABLE))

    def test_dongle_offline_is_unknown_immediately(self):
        t = tracker(initial_state=ON, last_known=ON)
        tr = t.observe(st(True, connected=False), 50)
        self.assertEqual((tr.state, tr.at, tr.reason),
                         (UNKNOWN, 50, REASON_DONGLE_OFFLINE))

    def test_stale_data_is_unknown(self):
        t = tracker(initial_state=OFF, last_known=OFF)
        tr = t.observe(st(False, age=181), 50)
        self.assertEqual((tr.state, tr.reason), (UNKNOWN, REASON_STALE_DATA))

    def test_missing_grid_flag_is_unknown(self):
        t = tracker(initial_state=ON, last_known=ON)
        tr = t.observe(st(None), 50)
        self.assertEqual((tr.state, tr.reason), (UNKNOWN, REASON_NO_GRID_DATA))

    def test_missing_data_age_is_fresh(self):
        t = tracker()
        status = st(True)
        del status['data_age_seconds']
        self.assertEqual(t.observe(status, 0).state, ON)

    def test_leaving_unknown_is_immediate_and_keeps_previous_known(self):
        t = tracker(initial_state=UNKNOWN, last_known=OFF)
        tr = t.observe(st(True), 400)
        self.assertEqual((tr.state, tr.at, tr.previous, tr.previous_known),
                         (ON, 400, UNKNOWN, OFF))

    def test_reason_updates_while_unknown(self):
        t = tracker(initial_state=UNKNOWN, last_known=ON,
                    initial_reason='monitor_downtime')
        for now in (0, 60, 120):
            self.assertIsNone(t.observe(None, now))
        self.assertEqual(t.reason, REASON_RPI_UNREACHABLE)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd vps && $PY -m unittest test_grid_state -v`
Expected: ERROR `ModuleNotFoundError: No module named 'grid_state'`

- [ ] **Step 4: Implement `vps/grid_state.py`**

```python
"""
Effective grid state: on / off / unknown
Pure logic with injected time, no I/O
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

ON = 'on'
OFF = 'off'
UNKNOWN = 'unknown'

# Why the state is unknown
REASON_RPI_UNREACHABLE = 'rpi_unreachable'
REASON_DONGLE_OFFLINE = 'dongle_offline'
REASON_STALE_DATA = 'stale_data'
REASON_NO_GRID_DATA = 'no_grid_data'
REASON_MONITOR_DOWNTIME = 'monitor_downtime'


@dataclass(frozen=True)
class Transition:
    """Confirmed change of the effective state"""
    state: str
    at: float                      # unix time of the boundary
    previous: Optional[str]        # effective state before (None if never known)
    previous_known: Optional[str]  # last ON/OFF before this change
    reason: Optional[str] = None   # set when state is UNKNOWN


@dataclass(frozen=True)
class GridChange:
    """Grid change worth alerting about (ON <-> OFF)"""
    state: str                 # ON or OFF
    at: float                  # unix time of the boundary
    duration_s: Optional[int]  # length of the outage that just ended
    approximate: bool          # happened while there was no data


class StateTracker:
    """Turns RPi polls into the effective grid state"""

    def __init__(self, debounce_s: float, unreachable_threshold: int,
                 stale_after_s: float, initial_state: Optional[str] = None,
                 initial_since: float = 0.0, last_known: Optional[str] = None,
                 initial_reason: Optional[str] = None):
        self.debounce_s = debounce_s
        self.unreachable_threshold = unreachable_threshold
        self.stale_after_s = stale_after_s

        self.state = initial_state
        self.since = initial_since
        self.reason = initial_reason
        self.last_known = last_known

        self._failures = 0
        self._first_failure_at = 0.0
        self._pending: Optional[str] = None
        self._pending_since = 0.0

    def _raw_state(self, status: Dict) -> Tuple[str, Optional[str]]:
        """State reported by one RPi response"""
        if not status.get('connected', False):
            return UNKNOWN, REASON_DONGLE_OFFLINE
        if (status.get('data_age_seconds') or 0) > self.stale_after_s:
            return UNKNOWN, REASON_STALE_DATA
        available = (status.get('grid') or {}).get('available')
        if available is None:
            return UNKNOWN, REASON_NO_GRID_DATA
        return (ON if available else OFF), None

    def _switch(self, state: str, at: float,
                reason: Optional[str] = None) -> Transition:
        transition = Transition(state=state, at=at, previous=self.state,
                                previous_known=self.last_known, reason=reason)
        self.state = state
        self.since = at
        self.reason = reason
        if state in (ON, OFF):
            self.last_known = state
        self._pending = None
        self._pending_since = 0.0
        return transition

    def observe(self, status: Optional[Dict], now: float) -> Optional[Transition]:
        """Feed one poll result (None = RPi request failed)"""
        if status is None:
            self._failures += 1
            if self._failures == 1:
                self._first_failure_at = now
            if self._failures >= self.unreachable_threshold:
                if self.state != UNKNOWN:
                    return self._switch(UNKNOWN, self._first_failure_at,
                                        REASON_RPI_UNREACHABLE)
                self.reason = REASON_RPI_UNREACHABLE
            return None

        self._failures = 0
        raw, reason = self._raw_state(status)

        if raw == UNKNOWN:
            if self.state != UNKNOWN:
                return self._switch(UNKNOWN, now, reason)
            self.reason = reason
            return None

        # No known state yet, or leaving unknown: RPi already debounced
        if self.state is None or self.state == UNKNOWN:
            return self._switch(raw, now)

        if raw == self.state:
            self._pending = None
            self._pending_since = 0.0
            return None

        if self._pending != raw:
            self._pending = raw
            self._pending_since = now
            return None

        if now - self._pending_since >= self.debounce_s:
            return self._switch(raw, self._pending_since)
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd vps && $PY -m unittest test_grid_state -v`
Expected: `Ran 10 tests` … `OK`

- [ ] **Step 6: Commit**

```bash
git add vps/grid_state.py vps/test_grid_state.py vps/config.py
git commit -m "Add StateTracker: effective grid state on/off/unknown"
```

---

### Task 2: Statistics over intervals

**Files:**
- Create: `vps/stats.py`
- Test: `vps/test_stats.py`

**Interfaces:**
- Produces: `Interval(state: str, start: datetime, end: datetime, ongoing: bool=False)`; `PeriodStats(start, end, on: float, off: float, unknown: float, partial: bool)` with property `availability -> Optional[float]`; `Outage(start, end, ongoing, start_uncertain, end_uncertain)` with properties `seconds`, `lower_bound`; `OutageSummary(count, total, average, longest)`; `HeatRow(day: date, off: List[Optional[float]], unknown: List[Optional[float]])`.
- Produces functions: `availability_pct(on, off) -> Optional[float]`, `window_stats(intervals, start, end) -> PeriodStats`, `period_bounds(period, count, now) -> List[Tuple[datetime, datetime, bool]]`, `split_by_periods(intervals, period, count, now) -> List[PeriodStats]`, `outages(intervals, start, end) -> List[Outage]` (newest first), `outage_summary(outs) -> OutageSummary`, `hourly_heatmap(intervals, days, now) -> List[HeatRow]`; constant `KYIV_TZ`.
- Semantics: seconds not covered by any interval count as `unknown`; `period` is `'day' | 'week' | 'month'`.

- [ ] **Step 1: Write the failing tests**

Create `vps/test_stats.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vps && $PY -m unittest test_stats -v`
Expected: ERROR `ModuleNotFoundError: No module named 'stats'`

- [ ] **Step 3: Implement `vps/stats.py`**

```python
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
        return (self.end - self.start).total_seconds()

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


def _totals(intervals: Sequence[Interval], lo: datetime,
            hi: datetime) -> Tuple[float, float, float]:
    """Seconds of on/off/unknown in [lo, hi); uncovered time is unknown"""
    on = off = unknown = 0.0
    for iv in intervals:
        a = max(iv.start, lo)
        b = min(iv.end, hi)
        if b <= a:
            continue
        seconds = (b - a).total_seconds()
        if iv.state == 'on':
            on += seconds
        elif iv.state == 'off':
            off += seconds
        else:
            unknown += seconds
    span = max((hi - lo).total_seconds(), 0.0)
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
            span = (hi - lo).total_seconds()
            if span <= 0:  # skipped hour on the spring DST day
                off.append(0.0)
                unknown.append(0.0)
                continue
            _, off_s, unknown_s = _totals(intervals, lo, hi)
            off.append(off_s / span)
            unknown.append(unknown_s / span)
        rows.append(HeatRow(d, off, unknown))
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vps && $PY -m unittest test_stats -v`
Expected: `Ran 15 tests` … `OK`

- [ ] **Step 5: Commit**

```bash
git add vps/stats.py vps/test_stats.py
git commit -m "Add interval-based grid statistics (Kyiv calendar, unknown gaps)"
```

---

### Task 3: `grid_intervals` table and DB methods

**Files:**
- Modify: `vps/database.py` (`_create_tables` at lines 60-117; new section after `get_hourly_averages`, line ~197; new cleanup method next to `cleanup_old_data`, line ~350)
- Test: `vps/test_database_intervals.py`

**Interfaces:**
- Consumes: `stats.Interval`.
- Produces on `Database`: `get_open_interval() -> Optional[Dict]` (keys `state`, `started_at`, `last_seen_at`); `get_last_known_state() -> Optional[str]`; `heartbeat(now: datetime)`; `switch_state(state: str, at: datetime, now: datetime) -> Optional[Dict]` (closed interval: `state`, `started_at`, `ended_at`); `recover_gap(now: datetime, max_gap: timedelta) -> Optional[Tuple[datetime, datetime]]`; `get_intervals(start: datetime, end: datetime) -> List[Interval]`; `cleanup_status(days: int) -> int`; for migration: `count_intervals() -> int`, `get_state_samples() -> List[Dict]` (keys `timestamp`, `connected`, `grid_available`), `insert_intervals(rows: List[Dict])` (keys `state`, `started_at`, `ended_at`), `truncate_intervals()`.

- [ ] **Step 1: Start a throwaway Postgres for the integration tests**

Run (needs Docker/Rancher Desktop running):
```bash
docker run -d --rm --name lux-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:15
export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
```
If Docker is not available, continue: the tests below are skipped without `TEST_DATABASE_URL` — say so explicitly in the task report (they then must run before deployment, see Task 8).

- [ ] **Step 2: Write the failing tests**

Create `vps/test_database_intervals.py`:

```python
"""
grid_intervals DB layer (integration, needs PostgreSQL).
Run: TEST_DATABASE_URL=postgresql://... python -m unittest test_database_intervals
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

from database import Database

TEST_URL = os.environ.get('TEST_DATABASE_URL')
BASE = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def T(minutes):
    return BASE + timedelta(minutes=minutes)


@unittest.skipUnless(TEST_URL, "TEST_DATABASE_URL not set")
class GridIntervalsDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database(TEST_URL)
        cls.db.connect()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE grid_intervals, inverter_status RESTART IDENTITY")

    def _open_count(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM grid_intervals WHERE ended_at IS NULL")
                return cur.fetchone()[0]

    def test_first_switch_opens_interval(self):
        self.assertIsNone(self.db.switch_state('on', T(0), T(0)))
        open_iv = self.db.get_open_interval()
        self.assertEqual((open_iv['state'], open_iv['started_at']), ('on', T(0)))

    def test_switch_closes_previous(self):
        self.db.switch_state('on', T(0), T(0))
        closed = self.db.switch_state('off', T(10), T(12))
        self.assertEqual(closed, {'state': 'on', 'started_at': T(0), 'ended_at': T(10)})
        ivs = self.db.get_intervals(T(-60), T(60))
        self.assertEqual([(iv.state, iv.start, iv.ongoing) for iv in ivs],
                         [('on', T(0), False), ('off', T(10), True)])
        self.assertEqual(ivs[0].end, T(10))

    def test_boundary_before_open_start_is_clamped(self):
        self.db.switch_state('on', T(10), T(10))
        closed = self.db.switch_state('off', T(5), T(11))
        self.assertEqual(closed['ended_at'], T(10))
        self.assertEqual(self.db.get_open_interval()['started_at'], T(10))

    def test_only_one_open_interval(self):
        self.db.switch_state('on', T(0), T(0))
        self.db.switch_state('off', T(1), T(1))
        self.db.switch_state('unknown', T(2), T(2))
        self.assertEqual(self._open_count(), 1)

    def test_same_state_switch_is_heartbeat(self):
        self.db.switch_state('on', T(0), T(0))
        self.assertIsNone(self.db.switch_state('on', T(5), T(5)))
        self.assertEqual(self.db.get_open_interval()['last_seen_at'], T(5))

    def test_heartbeat_updates_last_seen(self):
        self.db.switch_state('on', T(0), T(0))
        self.db.heartbeat(T(3))
        self.assertEqual(self.db.get_open_interval()['last_seen_at'], T(3))

    def test_recover_gap_records_unknown(self):
        self.db.switch_state('on', T(0), T(0))
        self.db.heartbeat(T(1))
        gap = self.db.recover_gap(T(30), timedelta(minutes=3))
        self.assertEqual(gap, (T(1), T(30)))
        open_iv = self.db.get_open_interval()
        self.assertEqual((open_iv['state'], open_iv['started_at']), ('unknown', T(1)))
        ivs = self.db.get_intervals(T(-60), T(60))
        self.assertEqual((ivs[0].state, ivs[0].end), ('on', T(1)))

    def test_recover_gap_without_gap(self):
        self.db.switch_state('on', T(0), T(0))
        self.assertIsNone(self.db.recover_gap(T(2), timedelta(minutes=3)))
        self.assertEqual(self.db.get_open_interval()['state'], 'on')

    def test_last_known_skips_unknown(self):
        self.db.switch_state('on', T(0), T(0))
        self.db.switch_state('unknown', T(5), T(5))
        self.assertEqual(self.db.get_last_known_state(), 'on')

    def test_cleanup_status_deletes_old_rows(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO inverter_status (timestamp, connected)
                    VALUES (NOW() - INTERVAL '100 days', TRUE),
                           (NOW() - INTERVAL '1 day', TRUE)
                """)
        self.assertEqual(self.db.cleanup_status(90), 1)

    def test_migration_helpers(self):
        self.db.insert_intervals([
            {'state': 'on', 'started_at': T(0), 'ended_at': T(5)},
            {'state': 'off', 'started_at': T(5), 'ended_at': T(9)},
        ])
        self.assertEqual(self.db.count_intervals(), 2)
        self.assertIsNone(self.db.get_open_interval())
        self.db.truncate_intervals()
        self.assertEqual(self.db.count_intervals(), 0)

    def test_state_samples(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO inverter_status (timestamp, connected, grid_available)
                    VALUES (%s, TRUE, TRUE), (%s, FALSE, NULL)
                """, (T(5), T(0)))
        samples = self.db.get_state_samples()
        self.assertEqual([(s['timestamp'], s['connected'], s['grid_available'])
                          for s in samples],
                         [(T(0), False, None), (T(5), True, True)])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd vps && $PY -m unittest test_database_intervals -v`
Expected with Postgres: errors like `AttributeError: 'Database' object has no attribute 'switch_state'` / `relation "grid_intervals" does not exist`. Without Postgres: `OK (skipped=12)`.

- [ ] **Step 4: Implement the DB layer**

In `vps/database.py`:

1. Imports (top of file): change `from datetime import datetime, timedelta` and `from typing import Optional, List, Dict, Any` to

```python
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
```

and after `import config` add `from stats import Interval`.

2. In `_create_tables`, after the subscribers `cur.execute(...)` block (before `logger.info("Database tables created/verified")`) add:

```python
                # Таблиця інтервалів стану мережі
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS grid_intervals (
                        id SERIAL PRIMARY KEY,
                        state TEXT NOT NULL
                            CHECK (state IN ('on', 'off', 'unknown')),
                        started_at TIMESTAMPTZ NOT NULL,
                        ended_at TIMESTAMPTZ NULL,
                        last_seen_at TIMESTAMPTZ NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_grid_intervals_open
                    ON grid_intervals ((ended_at IS NULL)) WHERE ended_at IS NULL;

                    CREATE INDEX IF NOT EXISTS idx_grid_intervals_started
                    ON grid_intervals (started_at);
                """)
```

3. After `get_hourly_averages` add a new section:

```python
    # =========================================================================
    # GRID INTERVALS
    # =========================================================================

    def get_open_interval(self) -> Optional[Dict]:
        """Current (open) grid state interval"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT state, started_at, last_seen_at FROM grid_intervals
                    WHERE ended_at IS NULL
                """)
                row = cur.fetchone()
                return dict(row) if row else None

    def get_last_known_state(self) -> Optional[str]:
        """Latest 'on'/'off' state, ignoring unknown intervals"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT state FROM grid_intervals
                    WHERE state <> 'unknown'
                    ORDER BY started_at DESC
                    LIMIT 1
                """)
                row = cur.fetchone()
                return row[0] if row else None

    def heartbeat(self, now: datetime):
        """Mark the open interval as still observed"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE grid_intervals SET last_seen_at = %s
                    WHERE ended_at IS NULL
                """, (now,))

    def switch_state(self, state: str, at: datetime,
                     now: datetime) -> Optional[Dict]:
        """Close the open interval at `at` and open a new one in `state`.

        Returns the closed interval (state, started_at, ended_at) or None.
        """
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, state, started_at FROM grid_intervals
                    WHERE ended_at IS NULL
                    FOR UPDATE
                """)
                row = cur.fetchone()

                if row and row['state'] == state:
                    cur.execute("""
                        UPDATE grid_intervals SET last_seen_at = %s
                        WHERE id = %s
                    """, (now, row['id']))
                    return None

                closed = None
                boundary = at
                if row:
                    # Boundary never goes before the start of the open interval
                    boundary = max(at, row['started_at'])
                    cur.execute("""
                        UPDATE grid_intervals
                        SET ended_at = %s,
                            last_seen_at = GREATEST(last_seen_at, %s)
                        WHERE id = %s
                    """, (boundary, boundary, row['id']))
                    closed = {'state': row['state'],
                              'started_at': row['started_at'],
                              'ended_at': boundary}

                cur.execute("""
                    INSERT INTO grid_intervals (state, started_at, last_seen_at)
                    VALUES (%s, %s, %s)
                """, (state, boundary, max(now, boundary)))
                return closed

    def recover_gap(self, now: datetime,
                    max_gap: timedelta) -> Optional[Tuple[datetime, datetime]]:
        """Record monitoring downtime as unknown.

        If the open interval was last seen more than `max_gap` ago, close it
        at last_seen_at and open 'unknown' from there. Returns (start, now).
        """
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, state, last_seen_at FROM grid_intervals
                    WHERE ended_at IS NULL
                    FOR UPDATE
                """)
                row = cur.fetchone()
                if not row or now - row['last_seen_at'] <= max_gap:
                    return None

                gap_start = row['last_seen_at']
                if row['state'] == 'unknown':
                    cur.execute("""
                        UPDATE grid_intervals SET last_seen_at = %s
                        WHERE id = %s
                    """, (now, row['id']))
                else:
                    cur.execute("""
                        UPDATE grid_intervals SET ended_at = last_seen_at
                        WHERE id = %s
                    """, (row['id'],))
                    cur.execute("""
                        INSERT INTO grid_intervals (state, started_at, last_seen_at)
                        VALUES ('unknown', %s, %s)
                    """, (gap_start, now))
                return gap_start, now

    def get_intervals(self, start: datetime, end: datetime) -> List[Interval]:
        """Intervals overlapping [start, end); the open one ends at NOW()"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT state, started_at,
                           COALESCE(ended_at, NOW()) AS ended_at,
                           ended_at IS NULL AS ongoing
                    FROM grid_intervals
                    WHERE started_at < %s AND COALESCE(ended_at, NOW()) > %s
                    ORDER BY started_at
                """, (end, start))
                return [Interval(row['state'], row['started_at'],
                                 row['ended_at'], row['ongoing'])
                        for row in cur.fetchall()]

    def count_intervals(self) -> int:
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM grid_intervals")
                return cur.fetchone()[0]

    def get_state_samples(self) -> List[Dict]:
        """All stored samples (time, connected, grid flag), oldest first"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT timestamp, connected, grid_available
                    FROM inverter_status
                    ORDER BY timestamp
                """)
                return [dict(row) for row in cur.fetchall()]

    def insert_intervals(self, rows: List[Dict]):
        """Insert closed intervals (migration)"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO grid_intervals
                        (state, started_at, ended_at, last_seen_at)
                    VALUES (%s, %s, %s, %s)
                """, [(r['state'], r['started_at'], r['ended_at'], r['ended_at'])
                      for r in rows])

    def truncate_intervals(self):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE grid_intervals RESTART IDENTITY")
```

4. In the CLEANUP section, after `cleanup_old_data`, add:

```python
    def cleanup_status(self, days: int) -> int:
        """Remove inverter_status samples older than `days`"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM inverter_status
                    WHERE timestamp < NOW() - make_interval(days => %s)
                """, (days,))
                return cur.rowcount
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd vps && $PY -m unittest test_database_intervals -v`
Expected with Postgres: `Ran 12 tests` … `OK`. Without: `OK (skipped=12)` — report it.

Also run the earlier suites to make sure the new import works: `cd vps && $PY -m unittest test_stats test_grid_state test_timezone`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add vps/database.py vps/test_database_intervals.py
git commit -m "Add grid_intervals table and interval DB methods"
```

---

### Task 4: Backfill migration from samples

**Files:**
- Create: `vps/migrate_intervals.py`
- Test: `vps/test_migrate_intervals.py`

**Interfaces:**
- Consumes: `Database.count_intervals`, `get_state_samples`, `insert_intervals`, `truncate_intervals`.
- Produces: `sample_state(sample: Dict) -> str`, `samples_to_intervals(samples, max_gap=MAX_SAMPLE_GAP) -> List[Dict]` (closed intervals `state`, `started_at`, `ended_at`), `main(argv=None, db=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `vps/test_migrate_intervals.py`:

```python
"""
Backfill migration: inverter_status samples -> grid_intervals.
Run: python -m unittest test_migrate_intervals
"""

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from migrate_intervals import main, samples_to_intervals

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def T(minutes):
    return BASE + timedelta(minutes=minutes)


def s(minutes, grid=True, connected=True):
    return {'timestamp': T(minutes), 'connected': connected, 'grid_available': grid}


class SamplesToIntervalsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(samples_to_intervals([]), [])

    def test_merges_same_state_and_splits_on_change(self):
        ivs = samples_to_intervals([s(0), s(5), s(10, False), s(15, False), s(20)])
        self.assertEqual([(iv['state'], iv['started_at'], iv['ended_at']) for iv in ivs],
                         [('on', T(0), T(10)), ('off', T(10), T(20))])

    def test_gap_becomes_unknown(self):
        ivs = samples_to_intervals([s(0), s(5), s(60), s(65)])
        self.assertEqual([(iv['state'], iv['started_at'], iv['ended_at']) for iv in ivs],
                         [('on', T(0), T(5)), ('unknown', T(5), T(60)),
                          ('on', T(60), T(65))])

    def test_disconnected_or_null_is_unknown(self):
        ivs = samples_to_intervals([s(0), s(5, connected=False), s(10, grid=None), s(15)])
        self.assertEqual([iv['state'] for iv in ivs], ['on', 'unknown', 'on'])


class MainTest(unittest.TestCase):
    def test_main_refuses_when_intervals_exist(self):
        db = MagicMock()
        db.count_intervals.return_value = 5
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([], db=db), 1)
        db.insert_intervals.assert_not_called()

    def test_dry_run_writes_nothing(self):
        db = MagicMock()
        db.count_intervals.return_value = 0
        db.get_state_samples.return_value = [s(0), s(5), s(10, False)]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['--dry-run'], db=db), 0)
        db.insert_intervals.assert_not_called()
        self.assertIn('intervals: 2', out.getvalue())

    def test_force_replaces_existing(self):
        db = MagicMock()
        db.count_intervals.return_value = 5
        db.get_state_samples.return_value = [s(0), s(5)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--force'], db=db), 0)
        db.truncate_intervals.assert_called_once()
        db.insert_intervals.assert_called_once()


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vps && $PY -m unittest test_migrate_intervals -v`
Expected: ERROR `ModuleNotFoundError: No module named 'migrate_intervals'`

- [ ] **Step 3: Implement `vps/migrate_intervals.py`**

```python
#!/usr/bin/env python3
"""
One-off backfill: build grid_intervals from inverter_status samples

Run with the public bot stopped:
    python migrate_intervals.py --dry-run   # show what would be written
    python migrate_intervals.py             # write (refuses if table not empty)
    python migrate_intervals.py --force     # wipe grid_intervals and rewrite
"""

import argparse
from datetime import timedelta
from typing import Dict, List, Optional, Sequence

# Larger gaps between 5-minute samples mean we did not observe the grid
MAX_SAMPLE_GAP = timedelta(minutes=10)


def sample_state(sample: Dict) -> str:
    if not sample.get('connected') or sample.get('grid_available') is None:
        return 'unknown'
    return 'on' if sample['grid_available'] else 'off'


def samples_to_intervals(samples: Sequence[Dict],
                         max_gap: timedelta = MAX_SAMPLE_GAP) -> List[Dict]:
    """Samples (oldest first) -> closed intervals; a change is placed at the
    first sample showing the new state"""
    intervals: List[Dict] = []
    current: Optional[Dict] = None
    prev_ts = None

    for sample in samples:
        ts = sample['timestamp']
        state = sample_state(sample)

        if current is not None and ts - prev_ts > max_gap:
            current['ended_at'] = prev_ts
            intervals.append(current)
            intervals.append({'state': 'unknown', 'started_at': prev_ts,
                              'ended_at': ts})
            current = None

        if current is None:
            current = {'state': state, 'started_at': ts}
        elif state != current['state']:
            current['ended_at'] = ts
            intervals.append(current)
            current = {'state': state, 'started_at': ts}
        prev_ts = ts

    if current is not None:
        current['ended_at'] = prev_ts
        intervals.append(current)

    # Merge neighbours with the same state (e.g. gap next to unknown samples)
    merged: List[Dict] = []
    for iv in intervals:
        if (merged and merged[-1]['state'] == iv['state']
                and merged[-1]['ended_at'] == iv['started_at']):
            merged[-1]['ended_at'] = iv['ended_at']
        else:
            merged.append(dict(iv))
    return [iv for iv in merged if iv['ended_at'] > iv['started_at']]


def main(argv=None, db=None) -> int:
    parser = argparse.ArgumentParser(description="Backfill grid_intervals")
    parser.add_argument('--dry-run', action='store_true',
                        help="print the result without writing")
    parser.add_argument('--force', action='store_true',
                        help="wipe grid_intervals before writing")
    args = parser.parse_args(argv)

    if db is None:
        from database import get_db
        db = get_db()

    existing = db.count_intervals()
    if existing and not (args.force or args.dry_run):
        print(f"grid_intervals already has {existing} rows; "
              f"use --force to rewrite (stop the bot first)")
        return 1

    samples = db.get_state_samples()
    intervals = samples_to_intervals(samples)

    totals = {'on': 0.0, 'off': 0.0, 'unknown': 0.0}
    for iv in intervals:
        totals[iv['state']] += (iv['ended_at'] - iv['started_at']).total_seconds()

    print(f"samples: {len(samples)}, intervals: {len(intervals)}")
    for state, seconds in totals.items():
        print(f"  {state}: {seconds / 3600:.1f} h")

    if args.dry_run:
        return 0

    if args.force:
        db.truncate_intervals()
    db.insert_intervals(intervals)
    print(f"written: {len(intervals)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vps && $PY -m unittest test_migrate_intervals -v`
Expected: `Ran 7 tests` … `OK`

- [ ] **Step 5: Commit**

```bash
git add vps/migrate_intervals.py vps/test_migrate_intervals.py
git commit -m "Add grid_intervals backfill migration from inverter_status"
```

---

### Task 5: Poller records intervals and reports unknown state

**Files:**
- Modify: `vps/poller.py` (whole file; keep `fetch_status`, `fetch_events`, `check_health`, `start`, `stop`, `get_poller` as they are)
- Test: `vps/test_poller.py`

**Interfaces:**
- Consumes: Task 1 (`StateTracker`, `Transition`, `GridChange`, constants), Task 3 DB methods (`recover_gap`, `get_open_interval`, `get_last_known_state`, `heartbeat`, `switch_state`, `save_status`, `cleanup_status`), `config.STALE_DATA_SECONDS`, `config.UNKNOWN_ALERT_AFTER`, `config.RETENTION_DAYS`.
- Produces: `RpiPoller(clock=time.time, db_factory=get_db)`; `init_state()`; `add_state_callback(cb)` where `cb(change: GridChange, status: Optional[dict])`; `add_unknown_callback(cb)` where `cb(active: bool, reason: Optional[str], seconds: int)` — `active=True`: unknown lasted ≥ `UNKNOWN_ALERT_AFTER`; `active=False, reason=None`: data back after such an alert; `active=False, reason='monitor_downtime'`: downtime found at startup; `get_last_status()`, `get_effective_state() -> Optional[str]`, `get_state_reason() -> Optional[str]`.
- Removes: `add_rpi_callback`, `get_grid_state`, `is_rpi_available`, `_check_state_change`, `_on_rpi_unavailable`, `_on_rpi_recovered`.

- [ ] **Step 1: Write the failing tests**

Create `vps/test_poller.py`:

```python
"""
Poller: interval recording, grid change callbacks, unknown alerts.
Run: python -m unittest test_poller
"""

import unittest
from datetime import datetime, timezone

from grid_state import GridChange
from poller import RpiPoller


def dt(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def st(available=True, connected=True):
    return {'connected': connected, 'data_age_seconds': 5,
            'grid': {'available': available, 'voltage': 230}}


class FakeDb:
    def __init__(self, open_iv=None, last_known=None, gap=None):
        self.open_iv = open_iv
        self.last_known = last_known
        self.gap = gap
        self.switches = []
        self.heartbeats = []
        self.saved = []
        self.cleanups = []
        self.fail_switch = 0

    def recover_gap(self, now, max_gap):
        return self.gap

    def get_open_interval(self):
        return self.open_iv

    def get_last_known_state(self):
        return self.last_known

    def heartbeat(self, now):
        self.heartbeats.append(now)

    def switch_state(self, state, at, now):
        if self.fail_switch:
            self.fail_switch -= 1
            raise RuntimeError("db down")
        closed = None
        if self.open_iv:
            closed = {'state': self.open_iv['state'],
                      'started_at': self.open_iv['started_at'], 'ended_at': at}
        self.switches.append((state, at))
        self.open_iv = {'state': state, 'started_at': at, 'last_seen_at': now}
        return closed

    def save_status(self, status):
        self.saved.append(status)

    def cleanup_status(self, days):
        self.cleanups.append(days)
        return 0


class Harness:
    def __init__(self, db):
        self.now = 0.0
        self.db = db
        self.poller = RpiPoller(clock=lambda: self.now, db_factory=lambda: db)
        self.changes = []
        self.unknown = []
        self.poller.add_state_callback(lambda c, s: self.changes.append(c))
        self.poller.add_unknown_callback(
            lambda a, r, sec: self.unknown.append((a, r, sec)))
        self.poller.init_state()

    def poll(self, t, status):
        self.now = t
        self.poller.fetch_status = lambda: status
        self.poller._poll_once()


def seeded(state, started=-1000, **kw):
    return FakeDb(open_iv={'state': state, 'started_at': dt(started),
                           'last_seen_at': dt(0)}, last_known=state, **kw)


class PollerTest(unittest.TestCase):
    def test_off_change_after_debounce_records_boundary(self):
        h = Harness(seeded('on'))
        for t in (0, 60, 120):
            h.poll(t, st(False))
        self.assertEqual(h.db.switches, [('off', dt(0))])
        self.assertEqual(h.changes, [GridChange('off', 0, None, False)])

    def test_on_after_off_reports_outage_duration(self):
        h = Harness(seeded('off', started=-600))
        for t in (0, 60, 120):
            h.poll(t, st(True))
        self.assertEqual(h.changes, [GridChange('on', 0, 600, False)])

    def test_unknown_alert_after_threshold_and_restore(self):
        h = Harness(seeded('on'))
        for t in (0, 60, 120):
            h.poll(t, None)
        self.assertEqual(h.unknown, [])
        h.poll(300, None)
        self.assertEqual(h.unknown, [(True, 'rpi_unreachable', 300)])
        h.poll(360, st(True))
        self.assertEqual(h.unknown[-1], (False, None, 360))
        self.assertEqual(h.changes, [])
        self.assertEqual(h.db.switches, [('unknown', dt(0)), ('on', dt(360))])

    def test_change_during_unknown_is_approximate(self):
        h = Harness(seeded('on'))
        h.poll(0, st(True, connected=False))
        h.poll(60, st(False))
        self.assertEqual(h.changes, [GridChange('off', 60, None, True)])

    def test_startup_gap_reports_monitor_downtime(self):
        db = FakeDb(open_iv={'state': 'unknown', 'started_at': dt(-3600),
                             'last_seen_at': dt(0)},
                    last_known='on', gap=(dt(-3600), dt(0)))
        h = Harness(db)
        self.assertEqual(h.unknown, [(False, 'monitor_downtime', 3600)])
        self.assertEqual(h.poller.get_effective_state(), 'unknown')
        h.poll(60, st(True))
        self.assertEqual(h.changes, [])
        self.assertEqual(len(h.unknown), 1)

    def test_quick_restart_only_heartbeats(self):
        h = Harness(seeded('on'))
        h.poll(0, st(True))
        self.assertEqual(h.db.switches, [])
        self.assertEqual(h.db.heartbeats, [dt(0)])
        self.assertEqual((h.changes, h.unknown), ([], []))

    def test_db_failure_does_not_block_alert_and_is_retried(self):
        db = seeded('on')
        db.fail_switch = 1
        h = Harness(db)
        for t in (0, 60, 120):
            h.poll(t, st(False))
        self.assertEqual(len(h.changes), 1)
        self.assertEqual(db.switches, [])
        h.poll(180, st(False))
        self.assertEqual(db.switches, [('off', dt(0))])

    def test_retention_runs_once_a_day(self):
        h = Harness(seeded('on'))
        h.poll(0, st(True))
        h.poll(60, st(True))
        self.assertEqual(h.db.cleanups, [90])
        h.poll(86400, st(True))
        self.assertEqual(h.db.cleanups, [90, 90])

    def test_empty_db_first_poll_opens_interval_without_alert(self):
        h = Harness(FakeDb())
        h.poll(0, st(True))
        self.assertEqual(h.db.switches, [('on', dt(0))])
        self.assertEqual(h.changes, [])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vps && $PY -m unittest test_poller -v`
Expected: FAIL/ERROR (e.g. `TypeError: RpiPoller.__init__() got an unexpected keyword argument 'clock'`)

- [ ] **Step 3: Rewrite the state part of `vps/poller.py`**

Replace the module docstring and imports (lines 1-16) with:

```python
"""
RPi API Poller
Fetches data from RPi, tracks the effective grid state (on/off/unknown)
and records it as intervals in the database
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Callable, List

import requests

import config
from database import get_db
from grid_state import (
    StateTracker, Transition, GridChange, ON, OFF, UNKNOWN,
    REASON_MONITOR_DOWNTIME,
)

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL = 24 * 3600


def _dt(ts: float) -> datetime:
    """Unix time -> aware UTC datetime"""
    return datetime.fromtimestamp(ts, timezone.utc)
```

Replace `__init__`, `add_state_callback`, `add_rpi_callback` (lines 22-53) with:

```python
    def __init__(self, clock: Callable[[], float] = time.time,
                 db_factory: Callable = get_db):
        self.api_url = config.RPI_API_URL
        self.api_token = config.RPI_API_TOKEN
        self.poll_interval = config.POLL_INTERVAL
        self.store_interval = config.STORE_INTERVAL

        self._clock = clock
        self._db_factory = db_factory

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_status: Optional[Dict] = None
        self._last_store_time: float = 0
        self._last_cleanup_time: Optional[float] = None

        self.tracker = self._new_tracker()
        self._unsaved: List[Transition] = []
        self._unknown_alerted = False
        self._unknown_since: float = 0

        # Callbacks
        self._state_callbacks: List[Callable] = []
        self._unknown_callbacks: List[Callable] = []

    def _new_tracker(self, **seed) -> StateTracker:
        return StateTracker(
            debounce_s=config.GRID_STATE_DEBOUNCE,
            unreachable_threshold=config.RPI_UNREACHABLE_THRESHOLD,
            stale_after_s=config.STALE_DATA_SECONDS,
            **seed,
        )

    def add_state_callback(self, callback: Callable):
        """Add callback(change: GridChange, status) for ON <-> OFF changes"""
        self._state_callbacks.append(callback)

    def add_unknown_callback(self, callback: Callable):
        """Add callback(active, reason, seconds) for unknown-state alerts"""
        self._unknown_callbacks.append(callback)
```

Replace everything from `def _check_state_change` (line 109) through the end of `_poll_once` (line 226) with:

```python
    def init_state(self):
        """Seed the tracker from the DB and record monitoring downtime"""
        now = self._clock()
        try:
            db = self._db_factory()
            gap = db.recover_gap(_dt(now),
                                 timedelta(seconds=3 * self.poll_interval))
            open_iv = db.get_open_interval()
            last_known = db.get_last_known_state()
        except Exception as e:
            logger.error(f"Failed to load grid state from DB: {e}")
            return

        if open_iv:
            self.tracker = self._new_tracker(
                initial_state=open_iv['state'],
                initial_since=open_iv['started_at'].timestamp(),
                last_known=last_known,
                initial_reason=REASON_MONITOR_DOWNTIME if gap else None,
            )
        else:
            self.tracker = self._new_tracker(last_known=last_known)

        if gap:
            gap_start, gap_end = gap
            seconds = int((gap_end - gap_start).total_seconds())
            logger.warning(f"Monitoring was down for {seconds}s, "
                           f"recorded as unknown")
            self._fire_unknown(False, REASON_MONITOR_DOWNTIME, seconds)

    def _fire_unknown(self, active: bool, reason: Optional[str], seconds: int):
        for callback in self._unknown_callbacks:
            try:
                callback(active, reason, seconds)
            except Exception as e:
                logger.error(f"Unknown-state callback error: {e}")

    def _record(self, transition: Optional[Transition],
                now: float) -> Optional[Dict]:
        """Write state changes (retrying earlier failures) or a heartbeat"""
        if transition is not None:
            self._unsaved.append(transition)

        db = self._db_factory()
        closed = None
        while self._unsaved:
            t = self._unsaved[0]
            closed = db.switch_state(t.state, _dt(t.at), _dt(now))
            self._unsaved.pop(0)

        if transition is None and self.tracker.state is not None:
            db.heartbeat(_dt(now))
        return closed

    def _on_transition(self, t: Transition, closed: Optional[Dict]):
        logger.info(f"Grid state: {t.previous} -> {t.state}"
                    + (f" ({t.reason})" if t.reason else ""))

        if t.state == UNKNOWN:
            self._unknown_since = t.at
        elif t.previous == UNKNOWN and self._unknown_alerted:
            self._unknown_alerted = False
            self._fire_unknown(False, None, int(t.at - self._unknown_since))

        if t.state not in (ON, OFF) or t.previous_known in (None, t.state):
            return

        duration = None
        if t.state == ON and closed and closed['state'] == OFF:
            duration = int(t.at - closed['started_at'].timestamp())

        change = GridChange(state=t.state, at=t.at, duration_s=duration,
                            approximate=(t.previous == UNKNOWN))
        for callback in self._state_callbacks:
            try:
                callback(change, self._last_status)
            except Exception as e:
                logger.error(f"State callback error: {e}")

    def _check_unknown_alert(self, now: float):
        if (self.tracker.state == UNKNOWN and not self._unknown_alerted
                and now - self.tracker.since >= config.UNKNOWN_ALERT_AFTER):
            self._unknown_alerted = True
            self._unknown_since = self.tracker.since
            self._fire_unknown(True, self.tracker.reason,
                               int(now - self.tracker.since))

    def _maybe_store(self, status: Dict, now: float):
        if now - self._last_store_time < self.store_interval:
            return
        try:
            self._db_factory().save_status(status)
            self._last_store_time = now
        except Exception as e:
            logger.error(f"Failed to store status: {e}")

    def _maybe_cleanup(self, now: float):
        if (self._last_cleanup_time is not None
                and now - self._last_cleanup_time < CLEANUP_INTERVAL):
            return
        self._last_cleanup_time = now
        try:
            deleted = self._db_factory().cleanup_status(config.RETENTION_DAYS)
            logger.info(f"Retention: removed {deleted} old status rows")
        except Exception as e:
            logger.error(f"Retention cleanup failed: {e}")

    def _poll_once(self):
        """Single poll iteration"""
        now = self._clock()
        status = self.fetch_status()
        if status is not None:
            self._last_status = status

        transition = self.tracker.observe(status, now)

        closed = None
        try:
            closed = self._record(transition, now)
        except Exception as e:
            logger.error(f"Failed to record grid state: {e}")

        if transition is not None:
            self._on_transition(transition, closed)

        self._check_unknown_alert(now)

        if status is not None:
            self._maybe_store(status, now)
        self._maybe_cleanup(now)
```

In `_run_loop`, insert `self.init_state()` right after the `logger.info(f"Poller started ...")` line.

Replace `get_grid_state` and `is_rpi_available` (lines 262-268) with:

```python
    def get_effective_state(self) -> Optional[str]:
        """'on' / 'off' / 'unknown', or None before the first poll"""
        return self.tracker.state

    def get_state_reason(self) -> Optional[str]:
        """Why the state is unknown (None otherwise)"""
        return self.tracker.reason
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vps && $PY -m unittest test_poller -v`
Expected: `Ran 9 tests` … `OK`

- [ ] **Step 5: Commit**

```bash
git add vps/poller.py vps/test_poller.py
git commit -m "Poller: record grid intervals, unknown-state alerts, retention"
```

Note: `bot.py`/`alerts.py` still reference removed poller methods until Tasks 6–7; do not deploy between tasks.

---

### Task 6: Alerts on GridChange and unknown state

**Files:**
- Modify: `vps/alerts.py`
- Test: `vps/test_alerts.py`

**Interfaces:**
- Consumes: `GridChange`, `ON`, reason constants from `grid_state`; `Database.save_event`, `get_active_subscribers`, `remove_subscriber`.
- Produces: `UNKNOWN_REASONS_UA: Dict[str, str]`; `format_seconds(seconds: int) -> str`; `AlertManager(public_bot, private_bot=None)` with `set_event_loop(loop)`, `on_grid_change(change, status)`, `on_unknown_change(active, reason, seconds)`, `async send_grid_alert(change, status)`; constants `SEND_OK`, `SEND_BLOCKED`, `SEND_ERROR`.
- Removes: `on_grid_state_change`, `on_rpi_state_change`, `_send_rpi_alert`.

- [ ] **Step 1: Write the failing tests**

Create `vps/test_alerts.py`:

```python
"""
AlertManager: save-first, blocked subscribers, owner detail, unknown alerts.
Run: python -m unittest test_alerts
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import Forbidden, TelegramError

import alerts
from alerts import AlertManager
from grid_state import GridChange

STATUS = {'grid': {'available': True, 'voltage': 231},
          'battery': {'soc': 80}, 'output': {}, 'temperature': {}}


def bot(side_effect=None):
    b = MagicMock()
    b.send_message = AsyncMock(side_effect=side_effect)
    return b


class GridAlertTest(unittest.TestCase):
    def run_alert(self, am, db, change):
        with patch.object(alerts, 'get_db', return_value=db), \
             patch.object(alerts.config, 'OWNER_CHAT_ID', 42), \
             patch.object(alerts.config, 'PUBLIC_CHANNEL_ID', ''):
            asyncio.run(am.send_grid_alert(change, STATUS))

    def test_event_saved_before_sending(self):
        calls = []
        db = MagicMock()
        db.save_event.side_effect = lambda *a, **k: calls.append('save')
        db.get_active_subscribers.return_value = [1]
        public = bot(side_effect=lambda **k: calls.append('send'))
        self.run_alert(AlertManager(public), db, GridChange('off', 0, None, False))
        self.assertEqual(calls, ['save', 'send'])

    def test_event_saved_even_if_sending_fails(self):
        db = MagicMock()
        db.get_active_subscribers.return_value = [1, 2]
        public = bot(side_effect=TelegramError("boom"))
        self.run_alert(AlertManager(public), db, GridChange('off', 0, None, False))
        db.save_event.assert_called_once()
        self.assertEqual(public.send_message.await_count, 2)

    def test_blocked_subscriber_is_deactivated(self):
        db = MagicMock()
        db.get_active_subscribers.return_value = [1, 2]
        public = bot(side_effect=[Forbidden("bot was blocked by the user"), None])
        self.run_alert(AlertManager(public), db, GridChange('on', 0, 600, False))
        db.remove_subscriber.assert_called_once_with(1)

    def test_owner_gets_detail_via_private_bot(self):
        db = MagicMock()
        db.get_active_subscribers.return_value = []
        private = bot()
        self.run_alert(AlertManager(bot(), private), db,
                       GridChange('on', 0, 600, False))
        private.send_message.assert_awaited_once()
        self.assertEqual(private.send_message.await_args.kwargs['chat_id'], 42)

    def test_message_has_duration_and_approximate_note(self):
        am = AlertManager(bot())
        text = am._format_grid_message(True, STATUS, 3900, approximate=True)
        self.assertIn("1 год 5 хв", text)
        self.assertIn("приблизний", text)


class UnknownAlertTest(unittest.TestCase):
    def send(self, *args):
        private = bot()
        with patch.object(alerts.config, 'OWNER_CHAT_ID', 42):
            asyncio.run(AlertManager(bot(), private)._send_unknown_alert(*args))
        return private.send_message.await_args.kwargs['text']

    def test_unknown_alert_has_reason(self):
        text = self.send(True, 'dongle_offline', 300)
        self.assertIn("WiFi-донгл", text)
        self.assertIn("5 хв", text)

    def test_restored_alert(self):
        self.assertIn("знову надходять", self.send(False, None, 600))

    def test_monitor_downtime_alert(self):
        self.assertIn("Моніторинг не працював", self.send(False, 'monitor_downtime', 7200))


class RunAsyncTest(unittest.TestCase):
    def test_uses_threadsafe_dispatch_when_loop_running(self):
        loop = MagicMock()
        loop.is_running.return_value = True
        am = AlertManager(bot())
        am.set_event_loop(loop)

        async def noop():
            pass

        coro = noop()
        with patch.object(alerts.asyncio, 'run_coroutine_threadsafe') as m:
            am._run_async(coro)
        m.assert_called_once_with(coro, loop)
        coro.close()


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vps && $PY -m unittest test_alerts -v`
Expected: failures/errors — e.g. `AttributeError: 'AlertManager' object has no attribute '_send_unknown_alert'`, `AttributeError: ... 'format_seconds'` style errors, and `TypeError`/`AttributeError` from `send_grid_alert` receiving a `GridChange`.

- [ ] **Step 3: Implement the alert changes in `vps/alerts.py`**

Replace the imports block (lines 6-26) with:

```python
import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict
from zoneinfo import ZoneInfo

# Kyiv timezone (EET/EEST, follows DST)
KYIV_TZ = ZoneInfo("Europe/Kyiv")


def kyiv_now() -> datetime:
    """Get current time in Kyiv timezone"""
    return datetime.now(KYIV_TZ)

from telegram import Bot
from telegram.error import Forbidden, TelegramError

import config
from database import get_db
from grid_state import (
    GridChange, ON,
    REASON_RPI_UNREACHABLE, REASON_DONGLE_OFFLINE, REASON_STALE_DATA,
    REASON_NO_GRID_DATA, REASON_MONITOR_DOWNTIME,
)

logger = logging.getLogger(__name__)

SEND_OK = 'ok'
SEND_BLOCKED = 'blocked'
SEND_ERROR = 'error'

UNKNOWN_REASONS_UA = {
    REASON_RPI_UNREACHABLE: "RPi недоступний",
    REASON_DONGLE_OFFLINE: "інвертор не відповідає (WiFi-донгл офлайн)",
    REASON_STALE_DATA: "дані застарілі",
    REASON_NO_GRID_DATA: "інвертор не повідомляє стан мережі",
    REASON_MONITOR_DOWNTIME: "моніторинг не працював",
}


def format_seconds(seconds: int) -> str:
    """'2 год 5 хв' / '5 хв 3 сек' / '40 сек'"""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} год {minutes} хв" if minutes else f"{hours} год"
    if minutes:
        return f"{minutes} хв {secs} сек" if secs else f"{minutes} хв"
    return f"{secs} сек"
```

Replace `_format_grid_message` (lines 41-63) with:

```python
    def _format_grid_message(self, grid_on: bool, status: Dict,
                             duration: int = None,
                             approximate: bool = False) -> str:
        """Format grid state change message"""
        if grid_on:
            voltage = (status.get('grid') or {}).get('voltage', 0)
            msg = f"⚡ Електромережу УВІМКНЕНО\nНапруга: {voltage}V"
            if duration:
                msg += f"\nВідключення тривало: {format_seconds(duration)}"
        else:
            msg = "❌ Електромережу ВИМКНЕНО"

        if approximate:
            msg += "\n(зміна сталася, поки не було даних — час приблизний)"

        msg += f"\n\n{kyiv_now().strftime('%H:%M:%S %d.%m.%Y')}"
        return msg
```

Replace `_send_message_async`, `_run_async`, `send_grid_alert`, `on_grid_state_change`, `on_rpi_state_change`, `_send_rpi_alert` (lines 109-217) with:

```python
    async def _send_message_async(self, bot: Bot, chat_id: int,
                                  text: str) -> str:
        """Send message; returns SEND_OK / SEND_BLOCKED / SEND_ERROR"""
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return SEND_OK
        except Forbidden as e:
            logger.warning(f"Chat {chat_id} blocked the bot: {e}")
            return SEND_BLOCKED
        except TelegramError as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return SEND_ERROR

    def _run_async(self, coro):
        """Run coroutine on the bot's event loop (called from poller thread)"""
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            logger.warning("Bot event loop not running, sending synchronously")
            asyncio.run(coro)

    async def send_grid_alert(self, change: GridChange, status: Optional[Dict]):
        """Save the event, then notify subscribers, channel and owner"""
        status = status or {}
        grid_on = change.state == ON
        db = get_db()

        try:
            db.save_event(
                'grid_on' if grid_on else 'grid_off',
                {
                    'voltage': (status.get('grid') or {}).get('voltage'),
                    'duration_seconds': change.duration_s,
                    'approximate': change.approximate,
                }
            )
        except Exception as e:
            logger.error(f"Failed to save grid event: {e}")

        message = self._format_grid_message(grid_on, status, change.duration_s,
                                            change.approximate)

        try:
            subscribers = db.get_active_subscribers()
        except Exception as e:
            logger.error(f"Failed to load subscribers: {e}")
            subscribers = []

        logger.info(f"Sending grid alert to {len(subscribers)} subscribers")

        for chat_id in subscribers:
            result = await self._send_message_async(self.public_bot, chat_id,
                                                     message)
            if result == SEND_BLOCKED:
                try:
                    db.remove_subscriber(chat_id)
                    logger.info(f"Deactivated subscriber {chat_id}")
                except Exception as e:
                    logger.error(f"Failed to deactivate {chat_id}: {e}")

        # Send to public channel if configured
        if config.PUBLIC_CHANNEL_ID:
            try:
                channel_id = int(config.PUBLIC_CHANNEL_ID)
                await self._send_message_async(self.public_bot, channel_id, message)
            except ValueError:
                pass

        # Detailed status to owner via private bot
        if self.private_bot and config.OWNER_CHAT_ID and status:
            await self._send_message_async(
                self.private_bot, config.OWNER_CHAT_ID,
                self._format_private_status(status)
            )

    def on_grid_change(self, change: GridChange, status: Optional[Dict]):
        """Poller callback: grid ON <-> OFF"""
        self._run_async(self.send_grid_alert(change, status))

    def on_unknown_change(self, active: bool, reason: Optional[str],
                          seconds: int):
        """Poller callback: unknown state alert / data restored / downtime"""
        self._run_async(self._send_unknown_alert(active, reason, seconds))

    async def _send_unknown_alert(self, active: bool, reason: Optional[str],
                                  seconds: int):
        """Unknown-state alerts go to the owner only"""
        if not config.OWNER_CHAT_ID:
            return
        bot = self.private_bot or self.public_bot
        now = kyiv_now().strftime('%H:%M:%S %d.%m.%Y')
        duration = format_seconds(seconds)

        if active:
            reason_text = UNKNOWN_REASONS_UA.get(reason, reason or "невідомо")
            msg = (f"\U0001f7e1 Немає даних про мережу\n"
                   f"Причина: {reason_text}\n"
                   f"Вже {duration}\n\n{now}")
        elif reason == REASON_MONITOR_DOWNTIME:
            msg = (f"⚠️ Моніторинг не працював {duration}\n"
                   f"Стан мережі за цей час невідомий\n\n{now}")
        else:
            msg = (f"✅ Дані знову надходять\n"
                   f"Не було даних: {duration}\n\n{now}")

        await self._send_message_async(bot, config.OWNER_CHAT_ID, msg)
```

`_format_private_status` and `send_status_to_owner` stay unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vps && $PY -m unittest test_alerts -v`
Expected: `Ran 9 tests` … `OK`

- [ ] **Step 5: Commit**

```bash
git add vps/alerts.py vps/test_alerts.py
git commit -m "Alerts: save event first, drop blocked subscribers, unknown-state alerts"
```

---

### Task 7: Bot handlers on intervals, message formatting, wiring

**Files:**
- Create: `vps/messages.py`
- Modify: `vps/bot.py` (imports 7-41, `format_duration` 44-64, `cmd_status` 105-158, `cmd_history` 161-184, `callback_history_detail` 187-215, grid block 252-373, `cmd_stats` 500-519, `run_public_bot` 537-576)
- Modify: `vps/database.py` (remove `get_grid_statistics`, `_get_grid_stats_by_period`, `get_daily_grid_stats`, `get_weekly_grid_stats`, `get_monthly_grid_stats`, `cleanup_old_data`)
- Modify: `vps/test_timezone.py` (`HistoryDetailTest`)
- Test: `vps/test_messages.py`, `vps/test_bot_handlers.py`

**Interfaces:**
- Consumes: Task 2 (`stats` as `grid_stats`), Task 3 (`get_intervals`, `get_open_interval`), Task 5 (`get_effective_state`, `get_state_reason`, `add_unknown_callback`, `start` / `stop`), Task 6 (`AlertManager.on_grid_change`, `on_unknown_change`, `UNKNOWN_REASONS_UA`).
- Produces in `messages.py`: `format_duration(seconds: float) -> str`, `make_bar(on, off, unknown, width=16) -> str`, `format_periods(periods, period) -> str`, `format_outages(outs, limit=10) -> str`, `format_history_summary(ws, outs) -> str`, `format_stats(rows) -> str` (rows: `List[Tuple[str, PeriodStats, OutageSummary]]`), `format_since(start, now) -> str`; constants `BAR_ON`, `BAR_OFF`, `BAR_UNKNOWN`.
- Produces in `bot.py`: `GRID_VIEWS = {'day': 7, 'week': 5, 'month': 12}`, `_grid_message(period: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `vps/test_messages.py`:

```python
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


if __name__ == '__main__':
    unittest.main()
```

Create `vps/test_bot_handlers.py`:

```python
"""
Bot handlers on interval statistics (empty DB, grid views, logging).
Run: python -m unittest test_bot_handlers
"""

import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from messages import BAR_UNKNOWN
from stats import Interval


class FakeDb:
    def __init__(self, intervals=()):
        self.intervals = list(intervals)

    def get_intervals(self, start, end):
        return [iv for iv in self.intervals if iv.start < end and iv.end > start]

    def get_open_interval(self):
        return None


def message_update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = bot.config.OWNER_CHAT_ID
    return update


class BotHandlersTest(unittest.TestCase):
    def run_cmd(self, handler, db):
        update = message_update()
        with patch.object(bot, 'get_db', return_value=db):
            asyncio.run(handler(update, MagicMock()))
        return update.message.reply_text.await_args.args[0]

    def test_httpx_does_not_log_requests(self):
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_history_on_empty_db(self):
        self.assertIn("відключень не було", self.run_cmd(bot.cmd_history, FakeDb()))

    def test_grid_on_empty_db_is_all_unknown(self):
        text = self.run_cmd(bot.cmd_grid, FakeDb())
        self.assertEqual(text.count(BAR_UNKNOWN * 16), 7)

    def test_year_view_has_twelve_months(self):
        with patch.object(bot, 'get_db', return_value=FakeDb()):
            text = bot._grid_message('month')
        self.assertIn("за рік", text)
        self.assertEqual(text.count(BAR_UNKNOWN * 16), 12)

    def test_stats_reports_availability(self):
        now = datetime.now(timezone.utc)
        db = FakeDb([Interval('on', now - timedelta(days=2), now - timedelta(hours=2)),
                     Interval('off', now - timedelta(hours=2), now, ongoing=True)])
        text = self.run_cmd(bot.cmd_stats, db)
        self.assertIn("За 24 години", text)
        self.assertIn("Відключень: 1", text)
        self.assertIn("Доступність", text)


if __name__ == '__main__':
    unittest.main()
```

In `vps/test_timezone.py`, replace the whole `HistoryDetailTest` class with:

```python
class HistoryDetailTest(unittest.TestCase):
    def test_history_shows_kyiv_time_for_utc_timestamp(self):
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) \
            - timedelta(hours=3)
        db = MagicMock()
        db.get_intervals.return_value = [
            Interval('off', start, start + timedelta(hours=1)),
        ]
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock(callback_query=query)

        with patch.object(bot, 'get_db', return_value=db):
            asyncio.run(bot.callback_history_detail(update, MagicMock()))

        text = query.edit_message_text.call_args.args[0]
        expected = start.astimezone(bot.KYIV_TZ).strftime('%H:%M %d.%m')
        self.assertIn(expected, text)
```

and add `from stats import Interval` to its imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vps && $PY -m unittest test_messages test_bot_handlers test_timezone -v`
Expected: ERROR `ModuleNotFoundError: No module named 'messages'`, then (after messages exists) failures in bot handlers.

- [ ] **Step 3: Implement `vps/messages.py`**

```python
"""
Text formatting for bot replies: durations, availability bars, outage lists
"""

from datetime import datetime, timedelta
from typing import List, Sequence, Tuple
from zoneinfo import ZoneInfo

from stats import Outage, OutageSummary, PeriodStats, availability_pct

KYIV_TZ = ZoneInfo("Europe/Kyiv")

DAYS_UA = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']
MONTHS_UA = [
    '', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
    'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень'
]

BAR_ON = "█"
BAR_OFF = "░"
BAR_UNKNOWN = "▒"

PERIOD_TITLES = {'day': "за тиждень", 'week': "за місяць", 'month': "за рік"}


def format_duration(seconds: float) -> str:
    """Format duration: seconds -> minutes -> hours -> days (>7d)"""
    total_seconds = int(seconds)
    if total_seconds < 60:
        return f"{total_seconds} сек"

    total_minutes = total_seconds // 60
    days = total_seconds // 86400

    if days >= 7:
        return f"{days} дн."

    total_hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    if total_hours >= 1:
        if minutes > 0:
            return f"{total_hours} год {minutes} хв"
        return f"{total_hours} год"

    return f"{total_minutes} хв"


def make_bar(on: float, off: float, unknown: float, width: int = 16) -> str:
    """Visual bar: on=█, off=░, unknown=▒ proportional to time"""
    total = on + off + unknown
    if total <= 0:
        return BAR_UNKNOWN * width
    n_on = round(on / total * width)
    n_off = min(round(off / total * width), width - n_on)
    return BAR_ON * n_on + BAR_OFF * n_off + BAR_UNKNOWN * (width - n_on - n_off)


def _period_label(p: PeriodStats, period: str) -> str:
    start = p.start.astimezone(KYIV_TZ)
    if period == 'day':
        return f"{DAYS_UA[start.weekday()]} {start:%d.%m}"
    if period == 'week':
        week_end = start.date() + timedelta(days=6)
        return f"{start:%d.%m}-{week_end:%d.%m}"
    return f"{MONTHS_UA[start.month]} {start.year}"


def format_periods(periods: Sequence[PeriodStats], period: str) -> str:
    """Availability per Kyiv day/week/month with bars and totals"""
    if not periods:
        return "⚠ Недостатньо даних"

    lines = []
    tot_on = tot_off = tot_unknown = 0.0
    for p in periods:
        tot_on += p.on
        tot_off += p.off
        tot_unknown += p.unknown
        hours = f"{p.on / 3600:.1f}/{p.off / 3600:.1f}"
        if p.unknown >= 60:
            hours += f" ?{p.unknown / 3600:.1f}"
        mark = "*" if p.partial else ""
        lines.append(f"{_period_label(p, period)}{mark} "
                     f"{make_bar(p.on, p.off, p.unknown)} {hours}")

    summary = (f"Є світло: {format_duration(tot_on)}\n"
               f"Немає: {format_duration(tot_off)}")
    if tot_unknown >= 60:
        summary += f"\nНемає даних: {format_duration(tot_unknown)}"
    pct = availability_pct(tot_on, tot_off)
    if pct is not None:
        summary += f"\nДоступність: {pct:.1f}%"

    legend = f"{BAR_ON} є світло  {BAR_OFF} немає  {BAR_UNKNOWN} невідомо"
    note = "\n* — неповний період" if any(p.partial for p in periods) else ""
    title = f"\U0001f4ca Електромережа {PERIOD_TITLES[period]}"
    return (f"{title}\n\n" + "\n".join(lines)
            + f"\n\n{summary}\n\n{legend}{note}")


def format_outages(outs: Sequence[Outage], limit: int = 10) -> str:
    """Outage list, newest first; ongoing and lower-bound durations marked"""
    if not outs:
        return "✅ Відключень не було"

    lines = ["\U0001f4cb Відключення (найновіші зверху):", ""]
    for o in outs[:limit]:
        start = o.start.astimezone(KYIV_TZ)
        duration = format_duration(o.seconds)
        if o.lower_bound:
            duration = f"≥ {duration}"
        if o.ongoing:
            lines.append(f"\U0001f534 з {start:%H:%M %d.%m} — триває ({duration})")
            continue
        end = o.end.astimezone(KYIV_TZ)
        end_str = (f"{end:%H:%M}" if end.date() == start.date()
                   else f"{end:%H:%M %d.%m}")
        lines.append(f"❌ {start:%H:%M %d.%m} – {end_str} ({duration})")
    return "\n".join(lines)


def format_history_summary(ws: PeriodStats, outs: Sequence[Outage]) -> str:
    """/history: last 24 hours"""
    if not outs and ws.off == 0:
        message = "✅ За останні 24 години відключень не було!"
    else:
        message = (f"\U0001f4ca За 24 години:\n\n"
                   f"Відключень: {len(outs)}\n"
                   f"Без світла: {format_duration(ws.off)}")
    if ws.unknown >= 60:
        message += f"\nНемає даних: {format_duration(ws.unknown)}"
    return message


def format_stats(rows: List[Tuple[str, PeriodStats, OutageSummary]]) -> str:
    """/stats (owner): several windows"""
    parts = ["\U0001f4ca Статистика відключень"]
    for label, ws, summary in rows:
        block = [f"\n{label}:",
                 f"  Відключень: {summary.count}",
                 f"  Без світла: {format_duration(ws.off)}"]
        if summary.count:
            block.append(f"  Середнє: {format_duration(summary.average)}, "
                         f"найдовше: {format_duration(summary.longest)}")
        if ws.availability is not None:
            block.append(f"  Доступність: {ws.availability:.1f}%")
        if ws.unknown >= 60:
            block.append(f"  Немає даних: {format_duration(ws.unknown)}")
        parts.append("\n".join(block))
    return "\n".join(parts)


def format_since(start: datetime, now: datetime) -> str:
    """'З HH:MM (тривалість)'; date added when not today"""
    s = start.astimezone(KYIV_TZ)
    n = now.astimezone(KYIV_TZ)
    time_str = s.strftime('%H:%M') if s.date() == n.date() else s.strftime('%H:%M %d.%m')
    return f"З {time_str} ({format_duration((n - s).total_seconds())})"
```

- [ ] **Step 4: Update `vps/bot.py`**

1. Imports (lines 10-33): replace `from datetime import datetime, timedelta` with
`from datetime import datetime, timedelta, timezone`; change `from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup` to
`from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup`; replace `from alerts import AlertManager` with:

```python
from alerts import AlertManager, UNKNOWN_REASONS_UA
from grid_state import ON, OFF
import stats as grid_stats
from messages import (
    format_history_summary, format_outages, format_periods, format_since,
    format_stats,
)
```

2. After the `logging.basicConfig(...)` call (line 40) add:

```python
# httpx logs full Telegram URLs (with the bot token) at INFO
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
```

3. Delete `format_duration` (lines 44-64).

4. Replace `cmd_status` with:

```python
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    poller = get_poller()
    status = poller.get_last_status()
    state = poller.get_effective_state()

    if state is None:
        await update.message.reply_text(
            "⚠ Дані недоступні. Спробуйте пізніше."
        )
        return

    voltage = ((status or {}).get('grid') or {}).get('voltage', 0)
    if state == ON:
        message = f"✅ Електромережа: УВІМКНЕНО\nНапруга: {voltage}V"
    elif state == OFF:
        message = f"❌ Електромережа: ВИМКНЕНО\nНапруга: {voltage}V"
    else:
        reason = UNKNOWN_REASONS_UA.get(poller.get_state_reason(),
                                        "невідома причина")
        message = f"⚠️ Немає свіжих даних про мережу\nПричина: {reason}"

    try:
        open_iv = get_db().get_open_interval()
        if open_iv:
            message += "\n" + format_since(open_iv['started_at'],
                                           datetime.now(timezone.utc))
    except Exception as e:
        logger.warning(f"Failed to get open interval for status: {e}")

    message += f"\n\nОновлено: {kyiv_now().strftime('%H:%M:%S')}"
    await update.message.reply_text(message)
```

5. Replace `cmd_history` and `callback_history_detail` with:

```python
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command: last 24 hours"""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    intervals = get_db().get_intervals(start, now)
    message = format_history_summary(
        grid_stats.window_stats(intervals, start, now),
        grid_stats.outages(intervals, start, now),
    )

    keyboard = [[
        InlineKeyboardButton("Детальніше", callback_data="history_detail")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(message, reply_markup=reply_markup)


async def callback_history_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle history detail callback: outages of the last 7 days"""
    query = update.callback_query
    await query.answer()

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    intervals = get_db().get_intervals(start, now)
    await query.edit_message_text(
        format_outages(grid_stats.outages(intervals, start, now))
    )
```

6. Replace the whole GRID AVAILABILITY STATS block (`DAYS_UA` through the end of `callback_grid`) with:

```python
# =============================================================================
# GRID AVAILABILITY STATS
# =============================================================================

# Button period -> number of periods shown
GRID_VIEWS = {'day': 7, 'week': 5, 'month': 12}


def _grid_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Тиждень", callback_data="grid_day"),
        InlineKeyboardButton("Місяць", callback_data="grid_week"),
        InlineKeyboardButton("Рік", callback_data="grid_month"),
    ]])


def _grid_message(period: str) -> str:
    count = GRID_VIEWS[period]
    now = datetime.now(timezone.utc)
    first_start = grid_stats.period_bounds(period, count, now)[0][0]
    intervals = get_db().get_intervals(first_start, now)
    periods = grid_stats.split_by_periods(intervals, period, count, now)
    return format_periods(periods, period)


async def cmd_grid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /grid command — grid availability statistics"""
    await update.message.reply_text(_grid_message('day'),
                                    reply_markup=_grid_keyboard())


async def callback_grid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle grid view switching"""
    query = update.callback_query
    await query.answer()

    period = query.data.replace("grid_", "")
    if period not in GRID_VIEWS:
        return
    await query.edit_message_text(_grid_message(period),
                                  reply_markup=_grid_keyboard())
```

7. Replace `cmd_stats` with:

```python
@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command (private bot)"""
    now = datetime.now(timezone.utc)
    windows = [("За 24 години", timedelta(hours=24)),
               ("За 7 днів", timedelta(days=7)),
               ("За 30 днів", timedelta(days=30))]
    intervals = get_db().get_intervals(now - windows[-1][1], now)

    rows = []
    for label, length in windows:
        start = now - length
        rows.append((label,
                     grid_stats.window_stats(intervals, start, now),
                     grid_stats.outage_summary(
                         grid_stats.outages(intervals, start, now))))

    await update.message.reply_text(format_stats(rows))
```

8. Replace `run_public_bot` with:

```python
def run_public_bot():
    """Run public bot only"""
    logger.info("Starting public bot...")

    # Initialize database
    get_db()
    poller = get_poller()
    private_bot = Bot(config.PRIVATE_BOT_TOKEN)

    async def on_start(application: Application):
        """Wire alerts and start polling once the bot loop is running"""
        owner_bot = private_bot
        try:
            await private_bot.initialize()
        except Exception as e:
            logger.error(f"Private bot unavailable for owner alerts: {e}")
            owner_bot = None

        alert_manager = AlertManager(application.bot, owner_bot)
        alert_manager.set_event_loop(asyncio.get_running_loop())
        poller.add_state_callback(alert_manager.on_grid_change)
        poller.add_unknown_callback(alert_manager.on_unknown_change)
        poller.start()

    async def on_stop(application: Application):
        poller.stop()
        try:
            await private_bot.shutdown()
        except Exception as e:
            logger.warning(f"Private bot shutdown: {e}")

    app = (Application.builder()
           .token(config.PUBLIC_BOT_TOKEN)
           .post_init(on_start)
           .post_shutdown(on_stop)
           .build())

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("grid", cmd_grid))

    app.add_handler(CallbackQueryHandler(callback_history_detail, pattern="^history_"))
    app.add_handler(CallbackQueryHandler(callback_grid, pattern="^grid_"))

    logger.info("Public bot started")

    # Run
    app.run_polling(allowed_updates=Update.ALL_TYPES)

    close_db()
```

9. In `vps/database.py` delete `get_grid_statistics`, the whole `GRID AVAILABILITY STATS` section (`_get_grid_stats_by_period`, `get_daily_grid_stats`, `get_weekly_grid_stats`, `get_monthly_grid_stats`) and `cleanup_old_data`.

10. Check no references to removed names remain:

Run: `cd vps && grep -nE 'format_duration\(|is_rpi_available|get_grid_state|get_grid_statistics|add_rpi_callback|on_grid_state_change|on_rpi_state_change|get_(daily|weekly|monthly)_grid_stats|cleanup_old_data|_make_bar|_format_grid_stats' *.py | grep -v '^messages.py\|^test_'`
Expected: no output.

- [ ] **Step 5: Run the full suite**

Run: `cd vps && $PY -m unittest discover -s . -p 'test_*.py' -v`
Expected: all tests `OK` (DB integration tests skipped only if `TEST_DATABASE_URL` is unset — report it).

Also: `cd vps && $PY -m py_compile *.py && $PY -m pyflakes *.py` (install pyflakes into the venv if missing)
Expected: `py_compile` silent; pyflakes reports nothing about undefined names (unused-import warnings in untouched code are acceptable but list them in the report).

- [ ] **Step 6: Commit**

```bash
git add vps/messages.py vps/bot.py vps/database.py vps/test_messages.py vps/test_bot_handlers.py vps/test_timezone.py
git commit -m "Bot: interval-based /grid, /history, /stats, /status; hide tokens from logs"
```

---

### Task 8: Docs, final verification, deployment (owner-confirmed)

**Files:**
- Modify: `README.md` (VPS configuration table; new "Migration" subsection)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Update README**

In `README.md`, in the `### VPS (vps/.env)` table add rows after `RPI_UNREACHABLE_THRESHOLD`:

```markdown
| `STALE_DATA_SECONDS` | 180 | RPi data older than this → state "unknown" (🟡) |
| `UNKNOWN_ALERT_AFTER` | 300 | Seconds of "unknown" before the owner is alerted |
| `RETENTION_DAYS` | 90 | Days of `inverter_status` samples to keep |
```

and after the Configuration section add:

```markdown
### Grid state history (`grid_intervals`)

The poller records the grid state as intervals (`on` / `off` / `unknown`).
Statistics in `/grid`, `/history`, `/stats` are computed from them; time when
the monitor had no data is shown as "unknown" and excluded from availability %.

One-off backfill from existing samples (run with the public bot stopped):

    python migrate_intervals.py --dry-run
    python migrate_intervals.py
```

Commit: `git add README.md && git commit -m "Document grid intervals settings and migration"`

- [ ] **Step 2: Full local verification**

With a Postgres available (`TEST_DATABASE_URL` set, see Task 3 Step 1):
Run: `cd vps && $PY -m unittest discover -s . -p 'test_*.py'`
Expected: `OK` with **no** skipped DB tests. If Docker cannot be started, stop and ask the owner how to run the DB tests before deploying.

- [ ] **Step 3: Push and open a PR**

```bash
git push origin feature/grid-intervals-stats
gh pr create --repo TIrtxika/luxpower-grid-monitor --base main --head feature/grid-intervals-stats --title "Grid intervals and real statistics (stage 1)" --body-file pr-body.md
```
`pr-body.md` (temporary, not committed): a "Summary" list of the spec's stage-1 items, a "Test plan" with the exact `unittest` summary line from Step 2, and the attribution footer. Merging is the owner's action.

- [ ] **Step 4: Deploy to the VPS — each command only after the owner confirms**

The VPS app lives in `/opt/luxpower` (owner `luxpower`), units `luxpower-bot-public` / `luxpower-bot-private`; access via XPipe system `instance01 (mon+tg bot+wireguard)`.

1. Backups:
```bash
sudo cp -a /opt/luxpower /opt/luxpower.bak-$(date +%Y%m%d)
sudo -u postgres pg_dump luxpower > ~/luxpower-$(date +%F).sql && ls -lh ~/luxpower-$(date +%F).sql
```
2. Copy the new `vps/*.py` (not `test_*.py`, not `.env`) to `/tmp/luxpower-new/` on the VPS (scp or XPipe file transfer), then:
```bash
sudo install -o luxpower -g luxpower -m 644 /tmp/luxpower-new/*.py /opt/luxpower/
```
3. Find how the units load the environment: `systemctl cat luxpower-bot-public | grep -E 'EnvironmentFile|Environment='`
4. Stop the public bot and run the migration dry run (replace the env-file path with the one from step 3):
```bash
sudo systemctl stop luxpower-bot-public
sudo -u luxpower bash -c 'set -a; . /opt/luxpower/.env; set +a; cd /opt/luxpower && venv/bin/python migrate_intervals.py --dry-run'
```
Show the output to the owner; continue only with their approval.
5. Real migration, then start both bots:
```bash
sudo -u luxpower bash -c 'set -a; . /opt/luxpower/.env; set +a; cd /opt/luxpower && venv/bin/python migrate_intervals.py'
sudo systemctl restart luxpower-bot-public luxpower-bot-private
sleep 10; systemctl is-active luxpower-bot-public luxpower-bot-private
sudo journalctl -u luxpower-bot-public --since '-2 min' --no-pager | grep -vE 'getUpdates' | tail -30
```
Expected: both `active`; log shows `Poller started`, no tracebacks, and **no** `api.telegram.org/bot` URLs.
6. Owner checks `/status`, `/grid` (Тиждень/Місяць/Рік), `/history` → «Детальніше», `/stats` in the private bot.

Rollback: `sudo systemctl stop luxpower-bot-public luxpower-bot-private && sudo rsync -a --delete /opt/luxpower.bak-<date>/ /opt/luxpower/ && sudo systemctl start luxpower-bot-public luxpower-bot-private` (the `grid_intervals` table can stay; old code ignores it).
