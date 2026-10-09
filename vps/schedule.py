"""
Planned outage schedule for one group (YASNO / DTEK Kyiv)
Pure parsing and logic; fetching lives in fetch_schedule()

YASNO slots are minutes from local midnight (Kyiv wall clock) with type
"Definite" (planned outage) or "NotPlanned"; each day has a status.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple

import requests

from stats import KYIV_TZ

APPLIES = 'ScheduleApplies'
WAITING = 'WaitingForSchedule'
EMERGENCY = 'EmergencyShutdowns'
NO_OUTAGES = 'NoOutages'


@dataclass(frozen=True)
class PlannedOutage:
    start: datetime  # aware
    end: datetime


@dataclass(frozen=True)
class DaySchedule:
    day: date
    status: str
    outages: Tuple[PlannedOutage, ...] = ()


def _wall_clock(day: date, minutes: int) -> datetime:
    if minutes >= 1440:
        return datetime.combine(day + timedelta(days=1), time(0), KYIV_TZ)
    return datetime.combine(day, time(minutes // 60, minutes % 60), KYIV_TZ)


def parse_group(payload: Dict, group: str) -> List[DaySchedule]:
    """YASNO planned-outages payload -> today/tomorrow schedules of a group"""
    data = payload.get(group)
    if not data:
        return []
    result = []
    for key in ('today', 'tomorrow'):
        info = data.get(key)
        if not info or not info.get('date'):
            continue
        day = datetime.fromisoformat(info['date']).date()
        outages: List[PlannedOutage] = []
        for slot in sorted(info.get('slots', []), key=lambda s: s['start']):
            if slot.get('type') != 'Definite':
                continue
            start, end = _wall_clock(day, slot['start']), _wall_clock(day, slot['end'])
            if outages and outages[-1].end == start:
                outages[-1] = PlannedOutage(outages[-1].start, end)
            else:
                outages.append(PlannedOutage(start, end))
        result.append(DaySchedule(day, info.get('status', ''), tuple(outages)))
    return result


def day_to_dict(d: DaySchedule) -> Dict:
    return {'day': d.day.isoformat(), 'status': d.status,
            'outages': [[o.start.isoformat(), o.end.isoformat()] for o in d.outages]}


def day_from_dict(raw: Dict) -> DaySchedule:
    return DaySchedule(date.fromisoformat(raw['day']), raw['status'],
                       tuple(PlannedOutage(datetime.fromisoformat(a),
                                           datetime.fromisoformat(b))
                             for a, b in raw['outages']))


def applied_outages(days: Sequence[DaySchedule]) -> List[PlannedOutage]:
    """Outages of days where the schedule applies, merged across midnight"""
    merged: List[PlannedOutage] = []
    for o in sorted((o for d in days if d.status == APPLIES for o in d.outages),
                    key=lambda o: o.start):
        if merged and merged[-1].end >= o.start:
            merged[-1] = PlannedOutage(merged[-1].start, max(merged[-1].end, o.end))
        else:
            merged.append(o)
    return merged


def _when(start: datetime, now: datetime) -> str:
    delta = (start.astimezone(KYIV_TZ).date() - now.astimezone(KYIV_TZ).date()).days
    if delta == 0:
        return "сьогодні"
    if delta == 1:
        return "завтра"
    return start.astimezone(KYIV_TZ).strftime('%d.%m')


def _hm(t: datetime) -> str:
    return t.astimezone(KYIV_TZ).strftime('%H:%M')


def describe(days: Sequence[DaySchedule], now: datetime) -> Optional[str]:
    """One /status line about the schedule; None without data"""
    if not days:
        return None
    if days[0].status == EMERGENCY:
        return "⚠️ Діють аварійні відключення — графік не застосовується"
    upcoming = [o for o in applied_outages(days) if o.end > now]
    if upcoming:
        o = upcoming[0]
        if o.start <= now:
            return f"\U0001f4c5 За графіком світла немає до {_hm(o.end)}"
        return (f"\U0001f4c5 Наступне відключення за графіком: "
                f"{_when(o.start, now)} {_hm(o.start)}–{_hm(o.end)}")
    tomorrow = days[1] if len(days) > 1 else None
    if tomorrow is None or tomorrow.status == WAITING:
        return ("\U0001f4c5 Сьогодні відключень за графіком більше немає; "
                "графік на завтра ще не опубліковано")
    return "\U0001f4c5 Відключень за графіком не заплановано"


def due_reminders(days: Sequence[DaySchedule], now: datetime, lead: timedelta,
                  sent: Set[datetime]) -> List[PlannedOutage]:
    """Outages starting within `lead` that were not reminded yet (marks them)"""
    due = []
    for o in applied_outages(days):
        if o.start - lead <= now < o.start and o.start not in sent:
            sent.add(o.start)
            due.append(o)
    return due


def planned_seconds(days: Sequence[DaySchedule], start: datetime,
                    end: datetime) -> float:
    """Planned outage time inside [start, end)"""
    total = 0.0
    for o in applied_outages(days):
        a, b = max(o.start, start), min(o.end, end)
        if b > a:
            total += b.timestamp() - a.timestamp()
    return total


def fetch_schedule(url: str, group: str, timeout: float = 10,
                   session=None) -> List[DaySchedule]:
    """GET the YASNO planned-outages payload and parse one group"""
    response = (session or requests).get(
        url, timeout=timeout, headers={'User-Agent': 'luxpower-grid-monitor'})
    response.raise_for_status()
    return parse_group(response.json(), group)


def fetch_group(addresses_url: str, street_id: int, house_id: int,
                region_id: int = 25, dso_id: int = 902, timeout: float = 10,
                session=None) -> str:
    """Current group of an address, e.g. '16.1'"""
    response = (session or requests).get(
        f"{addresses_url}/group", timeout=timeout,
        headers={'User-Agent': 'luxpower-grid-monitor'},
        params={'regionId': region_id, 'dsoId': dso_id,
                'streetId': street_id, 'houseId': house_id})
    response.raise_for_status()
    data = response.json()
    return f"{data['group']}.{data['subgroup']}"
