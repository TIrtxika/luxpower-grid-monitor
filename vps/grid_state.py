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
