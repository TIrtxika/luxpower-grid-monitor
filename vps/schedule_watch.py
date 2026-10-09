"""
Planned outage schedule watcher, driven by the poller loop:
refreshes the group's schedule, fires reminders before planned outages
and periodically checks that the address still belongs to the group.
A failing or slow source never affects grid monitoring: network calls run
in a background thread, the poller only swaps in their result.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Set

from schedule import DaySchedule, due_reminders
from stats import KYIV_TZ

logger = logging.getLogger(__name__)


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


class ScheduleWatch:
    def __init__(self, group: str, fetch: Callable[[], List[DaySchedule]],
                 db_factory: Callable, refresh_s: float = 900,
                 lead: timedelta = timedelta(minutes=30),
                 group_check: Optional[Callable[[], str]] = None,
                 group_check_s: float = 7 * 86400,
                 late: timedelta = timedelta(minutes=5),
                 runner: Optional[Callable[[Callable], None]] = None):
        self.group = group
        self._fetch = fetch
        self._db_factory = db_factory
        self._refresh_s = refresh_s
        self._lead = lead
        self._group_check = group_check
        self._group_check_s = group_check_s
        self._late = late
        self._runner = runner or self.run_in_thread
        self._idle = threading.Event()
        self._idle.set()

        self._days: List[DaySchedule] = []
        self._last_fetch: Optional[float] = None
        self._last_group_check: Optional[float] = None
        self._reported_group: Optional[str] = None
        self._reminded: Set[datetime] = set()

        self._reminder_callbacks: List[Callable] = []
        self._group_callbacks: List[Callable] = []

    def add_reminder_callback(self, callback: Callable):
        """callback(outage: PlannedOutage) shortly before a planned outage"""
        self._reminder_callbacks.append(callback)

    def add_group_callback(self, callback: Callable):
        """callback(configured, actual) when the address moved to another group"""
        self._group_callbacks.append(callback)

    def days(self, now: datetime) -> List[DaySchedule]:
        """Known schedule from today (Kyiv) on"""
        today = now.astimezone(KYIV_TZ).date()
        return [d for d in self._days if d.day >= today]

    @staticmethod
    def run_in_thread(job: Callable):
        threading.Thread(target=job, daemon=True, name="schedule-fetch").start()

    def wait_idle(self, timeout: float) -> bool:
        """Wait for the background fetch to finish (tests, shutdown)"""
        return self._idle.wait(timeout)

    def tick(self, now: float):
        due = self._last_fetch is None or now - self._last_fetch >= self._refresh_s
        if due and self._idle.is_set():
            self._last_fetch = now
            self._idle.clear()
            try:
                self._runner(lambda: self._background(now))
            except Exception as e:
                self._idle.set()
                logger.error(f"Failed to start schedule fetch: {type(e).__name__}")
        self._remind(now)

    def _background(self, now: float):
        """Network work, off the poller's critical path"""
        try:
            self._refresh(now)
            if self._group_check and (self._last_group_check is None
                                      or now - self._last_group_check
                                      >= self._group_check_s):
                self._last_group_check = now
                self._check_group(now)
        finally:
            self._idle.set()

    def _refresh(self, now: float):
        try:
            days = self._fetch()
        except Exception as e:
            logger.warning(f"Schedule fetch failed: {type(e).__name__}")
            days = None
        if days:
            self._days = days
            try:
                self._db_factory().save_schedule(self.group, days, _dt(now))
            except Exception as e:
                logger.error(f"Failed to save schedule: {e}")
            return
        if days is not None:
            logger.warning(f"Schedule has no data for group {self.group}")
        if not self._days:
            # Fresh start with the source down: use what was stored
            today = _dt(now).astimezone(KYIV_TZ).date()
            try:
                self._days = self._db_factory().get_schedule(
                    self.group, today, today + timedelta(days=1))
            except Exception as e:
                logger.error(f"Failed to load schedule: {e}")

    def _remind(self, now: float):
        for outage in due_reminders(self._days, _dt(now), self._lead,
                                    self._reminded, late=self._late):
            logger.info(f"Planned outage reminder: {outage.start.isoformat()}")
            for callback in self._reminder_callbacks:
                try:
                    callback(outage)
                except Exception as e:
                    logger.error(f"Reminder callback error: {e}")

    def _check_group(self, now: float):
        try:
            actual = self._group_check()
        except Exception as e:
            logger.warning(f"Group check failed: {type(e).__name__}")
            # retry with the next refresh, not in a week
            self._last_group_check = now - self._group_check_s + self._refresh_s
            return
        if actual == self.group or actual == self._reported_group:
            return
        self._reported_group = actual
        logger.warning(f"Address group changed: {self.group} -> {actual}")
        for callback in self._group_callbacks:
            try:
                callback(self.group, actual)
            except Exception as e:
                logger.error(f"Group callback error: {e}")
