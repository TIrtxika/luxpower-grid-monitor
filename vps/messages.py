"""
Text formatting for bot replies: durations, availability bars, outage lists
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple
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


def format_plan_fact(label: str, planned_s: float, fact_s: float) -> str:
    """'📅 План / факт за 7 днів: за графіком 3 год, фактично 2 год 10 хв'"""
    def hours(s):
        return format_duration(s) if s >= 60 else "0 год"
    return (f"\U0001f4c5 План / факт {label}: за графіком {hours(planned_s)}, "
            f"фактично {hours(fact_s)} без світла")


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


TRAFFIC_LIGHT = {
    'on': "\U0001f7e2 Світло є",
    'off': "\U0001f534 Світла немає",
    'unknown': "\U0001f7e1 Невідомо",
}


def format_traffic_light(state: str, reason: Optional[str],
                         since: Optional[datetime], now: datetime,
                         voltage: Optional[float] = None,
                         data_age_s: Optional[float] = None) -> str:
    """Status header: 🟢 / 🔴 / 🟡 with since, voltage or data age"""
    header = TRAFFIC_LIGHT.get(state, TRAFFIC_LIGHT['unknown'])
    if state not in ('on', 'off') and reason:
        header += f" — {reason}"
    lines = [header]
    if since is not None:
        lines.append(format_since(since, now))
    if state in ('on', 'off'):
        if voltage is not None:
            lines.append(f"Напруга: {voltage}V")
    elif data_age_s is not None:
        lines.append(f"Останні дані: {format_duration(data_age_s)} тому")
    return "\n".join(lines)


def format_inverter_details(status: Dict) -> str:
    """Grid, battery, load, temperatures from an RPi status dict"""
    grid = status.get('grid') or {}
    battery = status.get('battery') or {}
    output = status.get('output') or {}
    temp = status.get('temperature') or {}
    return (
        f"⚡ Мережа: {grid.get('voltage', 0)}V / {grid.get('frequency', 0)}Hz\n\n"
        f"\U0001faab Батарея: {battery.get('soc', 0)}%\n"
        f"   Напруга: {battery.get('voltage', 0)}V\n"
        f"   Струм: {battery.get('current', 0)}A\n"
        f"   Потужність: {battery.get('power', 0)}W\n\n"
        f"\U0001f3e0 Навантаження: {output.get('load_power', 0)}W\n"
        f"   Вихід: {output.get('voltage', 0)}V / {output.get('frequency', 0)}Hz\n\n"
        f"\U0001f321 Температура:\n"
        f"   Інвертор: {temp.get('inverter', 0)}°C\n"
        f"   Радіатор: {temp.get('radiator', 0)}°C\n\n"
        f"DC Bus: {status.get('dc_bus_voltage', 0)}V"
    )


MODE_TEXT = {'all': "усі зміни", 'off_only': "лише відключення",
             'on_only': "лише повернення світла"}


def format_settings(s) -> str:
    """/settings text for subscriptions.Settings"""
    if s.quiet_enabled:
        quiet = (f"\U0001f515 Тихі години: {s.quiet_from:%H:%M}–{s.quiet_to:%H:%M} "
                 f"— сповіщення приходять беззвучно")
    else:
        quiet = "\U0001f514 Тихі години вимкнено"
    return ("⚙️ Налаштування сповіщень\n\n"
            f"{quiet}\n"
            f"\U0001f4e3 Надсилати: {MODE_TEXT.get(s.notify_mode, s.notify_mode)}\n"
            f"⏰ Нагадування за графіком: "
            f"{'увімк — за 30 хв до відключення' if s.remind_enabled else 'вимк'}")


def format_battery_line(fc) -> str:
    """'🪫 Батареї вистачить ще ~1 год 40 хв (−18%/год)' from battery.Forecast"""
    return (f"\U0001faab Батареї вистачить ще ~{format_duration(fc.seconds_left)} "
            f"(−{fc.rate_per_hour:.0f}%/год)")


def status_from_sample(row: Dict) -> Dict:
    """inverter_status DB row -> RPi-style status dict"""
    return {
        'grid': {'available': row.get('grid_available'),
                 'voltage': row.get('grid_voltage'),
                 'frequency': row.get('grid_frequency')},
        'battery': {'soc': row.get('battery_soc'),
                    'voltage': row.get('battery_voltage'),
                    'current': row.get('battery_current'),
                    'power': row.get('battery_power')},
        'output': {'load_power': row.get('load_power'),
                   'voltage': row.get('output_voltage'),
                   'frequency': row.get('output_frequency')},
        'temperature': {'inverter': row.get('inverter_temp'),
                        'radiator': row.get('radiator_temp')},
        'dc_bus_voltage': row.get('dc_bus_voltage'),
    }
