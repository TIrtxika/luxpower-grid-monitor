"""
Per-subscriber notification settings: quiet hours and notify mode
Pure logic; times are Kyiv wall-clock times
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

from stats import KYIV_TZ

MODES = ('all', 'off_only', 'on_only')

# Window choices offered in /settings
QUIET_WINDOWS = {
    '22-07': (time(22), time(7)),
    '23-07': (time(23), time(7)),
    '00-08': (time(0), time(8)),
}


@dataclass(frozen=True)
class Settings:
    chat_id: int
    quiet_enabled: bool = True
    quiet_from: time = time(23)
    quiet_to: time = time(7)
    notify_mode: str = 'all'
    remind_enabled: bool = True  # reminders before planned outages


def in_quiet_hours(s: Settings, now: datetime) -> bool:
    """Is `now` (aware) inside the subscriber's quiet window in Kyiv time?"""
    if not s.quiet_enabled:
        return False
    t = now.astimezone(KYIV_TZ).time()
    if s.quiet_from <= s.quiet_to:
        return s.quiet_from <= t < s.quiet_to
    # Window across midnight, e.g. 23:00-07:00
    return t >= s.quiet_from or t < s.quiet_to


def wants(s: Settings, grid_on: bool) -> bool:
    """Does the subscriber want this grid change?"""
    if s.notify_mode == 'off_only':
        return not grid_on
    if s.notify_mode == 'on_only':
        return grid_on
    return True


def window_key(s: Settings) -> Optional[str]:
    """Key of the matching offered window, None for a custom one"""
    for key, (start, end) in QUIET_WINDOWS.items():
        if (s.quiet_from, s.quiet_to) == (start, end):
            return key
    return None
