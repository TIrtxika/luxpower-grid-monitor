#!/usr/bin/env python3
"""
LuxPower Data Collector Service
Збирає дані з інвертора та зберігає в локальний кеш
"""

import time
import json
import logging
import signal
import sys
from pathlib import Path
from threading import Thread, Event
from dataclasses import asdict
from typing import Optional, Callable, List

from inverter import LuxPowerInverter, InverterStatus
import config

# Налаштування логування
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("collector")

# Шлях до файлу кешу
CACHE_FILE = Path("/tmp/luxpower_status.json")
EVENTS_FILE = Path("/tmp/luxpower_events.json")


class StateChangeDetector:
    """Детектор зміни стану мережі з debounce"""

    def __init__(self, debounce_seconds: int = 10):
        self.debounce_seconds = debounce_seconds
        self._last_grid_state: Optional[bool] = None
        self._pending_state: Optional[bool] = None
        self._pending_since: float = 0
        self._callbacks: List[Callable] = []

    def add_callback(self, callback: Callable):
        """Додати callback для сповіщення про зміну стану"""
        self._callbacks.append(callback)

    def update(self, grid_available: bool, timestamp: float):
        """Оновити стан та перевірити на зміну"""
        # Перше значення - просто запам'ятати
        if self._last_grid_state is None:
            self._last_grid_state = grid_available
            logger.info(f"Initial grid state: {'ON' if grid_available else 'OFF'}")
            return

        # Якщо стан не змінився
        if grid_available == self._last_grid_state:
            self._pending_state = None
            self._pending_since = 0
            return

        # Стан змінився - починаємо debounce
        if self._pending_state != grid_available:
            self._pending_state = grid_available
            self._pending_since = timestamp
            logger.debug(f"Pending state change to {'ON' if grid_available else 'OFF'}")
            return

        # Перевіряємо чи пройшов debounce
        if timestamp - self._pending_since >= self.debounce_seconds:
            old_state = self._last_grid_state
            self._last_grid_state = grid_available
            self._pending_state = None
            self._pending_since = 0

            logger.info(f"Grid state changed: {'ON' if old_state else 'OFF'} -> {'ON' if grid_available else 'OFF'}")

            # Виклик callbacks
            for callback in self._callbacks:
                try:
                    callback(old_state, grid_available, timestamp)
                except Exception as e:
                    logger.error(f"Callback error: {e}")


class EventLogger:
    """Логер подій для збереження історії"""

    def __init__(self, events_file: Path, max_events: int = 1000):
        self.events_file = events_file
        self.max_events = max_events
        self._events: List[dict] = []
        self._load()

    def _load(self):
        """Завантаження подій з файлу"""
        try:
            if self.events_file.exists():
                with open(self.events_file, 'r') as f:
                    self._events = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load events: {e}")
            self._events = []

    def _save(self):
        """Збереження подій у файл"""
        try:
            # Обмеження кількості подій
            if len(self._events) > self.max_events:
                self._events = self._events[-self.max_events:]

            with open(self.events_file, 'w') as f:
                json.dump(self._events, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save events: {e}")

    def log_event(self, event_type: str, data: dict):
        """Додати подію"""
        event = {
            "timestamp": time.time(),
            "type": event_type,
            "data": data
        }
        self._events.append(event)
        self._save()
        logger.info(f"Event logged: {event_type}")

    def get_events(self, event_type: Optional[str] = None,
                   since: Optional[float] = None) -> List[dict]:
        """Отримати події"""
        events = self._events

        if event_type:
            events = [e for e in events if e.get("type") == event_type]

        if since:
            events = [e for e in events if e.get("timestamp", 0) >= since]

        return events

    def get_last_event(self, event_type: str) -> Optional[dict]:
        """Останняя подія певного типу"""
        for event in reversed(self._events):
            if event.get("type") == event_type:
                return event
        return None


class DataCollector:
    """Основний сервіс збору даних"""

    def __init__(self):
        self.inverter = LuxPowerInverter(
            host=config.INVERTER_IP,
            port=config.INVERTER_PORT,
            dongle_serial=config.DONGLE_SERIAL,
            inverter_serial=config.INVERTER_SERIAL
        )

        self.state_detector = StateChangeDetector(
            debounce_seconds=config.STATE_CHANGE_DEBOUNCE
        )

        self.event_logger = EventLogger(EVENTS_FILE)

        self._stop_event = Event()
        self._last_status: Optional[InverterStatus] = None
        self._consecutive_failures = 0

        # Callback для зміни стану мережі
        self.state_detector.add_callback(self._on_grid_state_change)

    def _on_grid_state_change(self, old_state: bool, new_state: bool, timestamp: float):
        """Обробник зміни стану мережі"""
        event_type = "grid_on" if new_state else "grid_off"

        # Якщо мережа повернулася - обчислюємо тривалість відключення
        duration = None
        if new_state:
            last_off = self.event_logger.get_last_event("grid_off")
            if last_off:
                duration = int(timestamp - last_off["timestamp"])

        self.event_logger.log_event(event_type, {
            "grid_voltage": self._last_status.grid_voltage if self._last_status else 0,
            "duration_seconds": duration
        })

    def _save_status(self, status: InverterStatus):
        """Збереження статусу в кеш файл"""
        try:
            data = status.to_dict()
            with open(CACHE_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save status: {e}")

    def _collect_once(self) -> bool:
        """Одне опитування інвертора"""
        try:
            status = self.inverter.get_status(
                grid_threshold=config.GRID_VOLTAGE_THRESHOLD
            )

            if status.connected:
                self._last_status = status
                self._save_status(status)
                self._consecutive_failures = 0

                # Оновлення детектора стану
                self.state_detector.update(
                    status.grid_available,
                    status.timestamp
                )

                logger.debug(
                    f"Status: Grid={'ON' if status.grid_available else 'OFF'} "
                    f"{status.grid_voltage}V, SOC={status.battery_soc}%, "
                    f"Load={status.load_power}W"
                )
                return True
            else:
                self._consecutive_failures += 1
                logger.warning(f"Failed to get status (attempt {self._consecutive_failures})")
                return False

        except Exception as e:
            self._consecutive_failures += 1
            logger.error(f"Collection error: {e}")
            return False
        finally:
            self.inverter.disconnect()

    def run(self):
        """Основний цикл збору даних"""
        logger.info("Data collector started")
        logger.info(f"Inverter: {config.INVERTER_IP}:{config.INVERTER_PORT}")
        logger.info(f"Poll interval: {config.POLL_INTERVAL}s")

        while not self._stop_event.is_set():
            success = self._collect_once()

            # Якщо багато помилок підряд - збільшуємо інтервал
            if self._consecutive_failures >= config.MAX_RETRIES:
                logger.warning("Max retries reached, waiting longer...")
                self._stop_event.wait(config.RETRY_DELAY * 2)
            else:
                self._stop_event.wait(config.POLL_INTERVAL)

        logger.info("Data collector stopped")

    def stop(self):
        """Зупинка сервісу"""
        self._stop_event.set()
        self.inverter.disconnect()

    def get_status(self) -> Optional[InverterStatus]:
        """Отримання останнього статусу"""
        return self._last_status

    def get_events(self, event_type: Optional[str] = None,
                   since: Optional[float] = None) -> List[dict]:
        """Отримання подій"""
        return self.event_logger.get_events(event_type, since)


# Глобальний екземпляр для доступу з API
_collector: Optional[DataCollector] = None


def get_collector() -> Optional[DataCollector]:
    """Отримати екземпляр collector"""
    return _collector


def main():
    global _collector

    _collector = DataCollector()

    # Обробка сигналів для graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, stopping...")
        _collector.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Запуск
    _collector.run()


if __name__ == "__main__":
    main()
