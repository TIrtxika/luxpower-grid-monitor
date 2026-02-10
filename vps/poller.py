"""
RPi API Poller
Fetches data from RPi and stores in database
"""

import time
import logging
import threading
from typing import Optional, Dict, Callable, List

import requests

import config
from database import get_db

logger = logging.getLogger(__name__)


class RpiPoller:
    """Polls RPi API for inverter data"""

    def __init__(self):
        self.api_url = config.RPI_API_URL
        self.api_token = config.RPI_API_TOKEN
        self.poll_interval = config.POLL_INTERVAL
        self.store_interval = config.STORE_INTERVAL

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_status: Optional[Dict] = None
        self._last_store_time: float = 0
        self._last_grid_state: Optional[bool] = None

        # Debounce для зміни стану мережі
        self._pending_grid_state: Optional[bool] = None
        self._pending_since: float = 0

        # Трекінг доступності RPi
        self._consecutive_failures: int = 0
        self._rpi_available: bool = True
        self._rpi_unavailable_since: float = 0

        # Callbacks
        self._state_callbacks: List[Callable] = []
        self._rpi_callbacks: List[Callable] = []

    def add_state_callback(self, callback: Callable):
        """Add callback for grid state changes"""
        self._state_callbacks.append(callback)

    def add_rpi_callback(self, callback: Callable):
        """Add callback for RPi availability changes"""
        self._rpi_callbacks.append(callback)

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

    def _check_state_change(self, status: Dict):
        """Check for grid state changes with debounce to avoid false triggers"""
        grid_available = status.get('grid', {}).get('available')

        if grid_available is None:
            return

        # First status - just remember
        if self._last_grid_state is None:
            self._last_grid_state = grid_available
            logger.info(f"Initial grid state: {'ON' if grid_available else 'OFF'}")
            return

        # State matches confirmed state - reset any pending change
        if grid_available == self._last_grid_state:
            if self._pending_grid_state is not None:
                logger.info(f"Pending grid state change cancelled "
                           f"(was {'ON' if self._pending_grid_state else 'OFF'}, "
                           f"reverted after {time.time() - self._pending_since:.0f}s)")
                self._pending_grid_state = None
                self._pending_since = 0
            return

        # State differs from confirmed - start or continue debounce
        now = time.time()

        if self._pending_grid_state != grid_available:
            # New pending state detected
            self._pending_grid_state = grid_available
            self._pending_since = now
            logger.info(f"Pending grid state change to "
                       f"{'ON' if grid_available else 'OFF'}, "
                       f"waiting for debounce ({config.GRID_STATE_DEBOUNCE}s)")
            return

        # Same pending state - check if debounce period passed
        elapsed = now - self._pending_since
        if elapsed >= config.GRID_STATE_DEBOUNCE:
            old_state = self._last_grid_state
            self._last_grid_state = grid_available
            self._pending_grid_state = None
            self._pending_since = 0

            logger.info(f"Grid state confirmed after {elapsed:.0f}s: "
                       f"{'ON' if old_state else 'OFF'} -> "
                       f"{'ON' if grid_available else 'OFF'}")

            for callback in self._state_callbacks:
                try:
                    callback(old_state, grid_available, status)
                except Exception as e:
                    logger.error(f"State callback error: {e}")
        else:
            logger.debug(f"Debounce in progress: {elapsed:.0f}s / "
                        f"{config.GRID_STATE_DEBOUNCE}s")

    def _on_rpi_unavailable(self):
        """Handle RPi becoming unreachable"""
        self._rpi_available = False
        self._rpi_unavailable_since = time.time()

        # Скидаємо debounce — дані ненадійні
        if self._pending_grid_state is not None:
            logger.info("Resetting grid debounce due to RPi unavailability")
            self._pending_grid_state = None
            self._pending_since = 0

        logger.warning(f"RPi unreachable after {self._consecutive_failures} "
                      f"consecutive failures")

        for callback in self._rpi_callbacks:
            try:
                callback(False, self._consecutive_failures)
            except Exception as e:
                logger.error(f"RPi callback error: {e}")

    def _on_rpi_recovered(self):
        """Handle RPi becoming reachable again"""
        downtime = int(time.time() - self._rpi_unavailable_since)
        self._rpi_available = True
        self._rpi_unavailable_since = 0

        logger.info(f"RPi recovered after {downtime}s of downtime")

        for callback in self._rpi_callbacks:
            try:
                callback(True, downtime)
            except Exception as e:
                logger.error(f"RPi callback error: {e}")

    def _poll_once(self):
        """Single poll iteration"""
        status = self.fetch_status()

        if status is None:
            self._consecutive_failures += 1
            if (self._rpi_available and
                    self._consecutive_failures >= config.RPI_UNREACHABLE_THRESHOLD):
                self._on_rpi_unavailable()
            return

        # RPi відповів — скидаємо лічильник помилок
        if not self._rpi_available:
            self._on_rpi_recovered()
        self._consecutive_failures = 0

        self._last_status = status
        self._check_state_change(status)

        # Store to database periodically
        now = time.time()
        if now - self._last_store_time >= self.store_interval:
            try:
                db = get_db()
                db.save_status(status)
                self._last_store_time = now
            except Exception as e:
                logger.error(f"Failed to store status: {e}")

    def _run_loop(self):
        """Main polling loop"""
        logger.info(f"Poller started (interval: {self.poll_interval}s)")

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

    def get_grid_state(self) -> Optional[bool]:
        """Get current grid state"""
        return self._last_grid_state

    def is_rpi_available(self) -> bool:
        """Check if RPi is currently reachable"""
        return self._rpi_available


# Global instance
_poller: Optional[RpiPoller] = None


def get_poller() -> RpiPoller:
    """Get poller instance"""
    global _poller
    if _poller is None:
        _poller = RpiPoller()
    return _poller
