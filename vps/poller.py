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


class RpiPoller:
    """Polls RPi API for inverter data"""

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
        self._unknown_since: float = 0       # start of the unknown period
        self._unknown_alert_from: float = 0  # 🟡 alert threshold counts from here

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

    def _get_headers(self) -> Dict:
        """Get API request headers"""
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

    def fetch_status(self) -> Optional[Dict]:
        """Fetch current status from RPi"""
        try:
            response = requests.get(
                f"{self.api_url}/status",
                headers=self._get_headers(),
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch status: {e}")
            return None

    def fetch_events(self, since: float = None) -> Optional[List[Dict]]:
        """Fetch events from RPi"""
        try:
            params = {}
            if since:
                params['since'] = since

            response = requests.get(
                f"{self.api_url}/events",
                headers=self._get_headers(),
                params=params,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            return data.get('events', [])
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch events: {e}")
            return None

    def check_health(self) -> Dict:
        """Check RPi API health"""
        try:
            response = requests.get(
                f"{self.api_url}/health",
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Health check failed: {e}")
            return {"status": "error", "error": str(e)}

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

        if open_iv and open_iv['state'] == UNKNOWN:
            started = open_iv['started_at'].timestamp()
            self._unknown_since = started
            if gap:
                # The downtime message goes out below; give the 🟡 alert
                # a fresh threshold from now
                self._unknown_alert_from = now
                self._unknown_alerted = False
            else:
                # Quick restart: the owner was already alerted if it is old
                self._unknown_alert_from = started
                self._unknown_alerted = (now - started
                                         >= config.UNKNOWN_ALERT_AFTER)

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
            if db.heartbeat(_dt(now)) == 0:
                # Open interval vanished (e.g. table rewritten): reopen it
                logger.warning("No open grid interval, reopening current state")
                db.switch_state(self.tracker.state, _dt(self.tracker.since),
                                _dt(now))
        return closed

    def _on_transition(self, t: Transition, closed: Optional[Dict]):
        logger.info(f"Grid state: {t.previous} -> {t.state}"
                    + (f" ({t.reason})" if t.reason else ""))

        if t.state == UNKNOWN:
            self._unknown_since = t.at
            self._unknown_alert_from = t.at
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
                and now - self._unknown_alert_from >= config.UNKNOWN_ALERT_AFTER):
            self._unknown_alerted = True
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

    def _run_loop(self):
        """Main polling loop"""
        logger.info(f"Poller started (interval: {self.poll_interval}s)")
        self.init_state()

        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logger.error(f"Poll error: {e}")

            self._stop_event.wait(self.poll_interval)

        logger.info("Poller stopped")

    def start(self):
        """Start polling in background thread"""
        if self._thread and self._thread.is_alive():
            logger.warning("Poller already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop polling"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_last_status(self) -> Optional[Dict]:
        """Get last fetched status"""
        return self._last_status

    def get_effective_state(self) -> Optional[str]:
        """'on' / 'off' / 'unknown', or None before the first poll"""
        return self.tracker.state

    def get_state_reason(self) -> Optional[str]:
        """Why the state is unknown (None otherwise)"""
        return self.tracker.reason


# Global instance
_poller: Optional[RpiPoller] = None


def get_poller() -> RpiPoller:
    """Get poller instance"""
    global _poller
    if _poller is None:
        _poller = RpiPoller()
    return _poller
