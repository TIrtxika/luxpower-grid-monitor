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
    # Timestamps, not (n - s): both share one ZoneInfo, which would ignore DST
    return f"З {time_str} ({format_duration(now.timestamp() - start.timestamp())})"
