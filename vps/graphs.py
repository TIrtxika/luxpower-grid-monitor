"""
Graph generation for Telegram bot
Uses matplotlib to create charts
"""

import io
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from database import get_db

logger = logging.getLogger(__name__)


def create_voltage_chart(hours: int = 24) -> Optional[bytes]:
    """Create grid voltage chart"""
    db = get_db()
    data = db.get_status_history(hours=hours)

    if not data or len(data) < 2:
        return None

    try:
        timestamps = [row['timestamp'] for row in data]
        voltages = [row['grid_voltage'] or 0 for row in data]

        fig, ax = plt.subplots(figsize=(10, 5))

        ax.plot(timestamps, voltages, 'b-', linewidth=1)
        ax.fill_between(timestamps, voltages, alpha=0.3)

        # Grid availability zones
        for i, row in enumerate(data):
            if not row.get('grid_available', True):
                if i > 0:
                    ax.axvspan(timestamps[i-1], timestamps[i],
                              alpha=0.3, color='red')

        ax.set_xlabel('Час')
        ax.set_ylabel('Напруга (V)')
        ax.set_title(f'Напруга мережі за {hours} годин')

        ax.axhline(y=180, color='r', linestyle='--', alpha=0.5, label='Поріг')
        ax.axhline(y=220, color='g', linestyle='--', alpha=0.5, label='Норма')

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=max(1, hours // 12)))

        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.legend()
        plt.grid(True, alpha=0.3)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        plt.close(fig)

        return buf.getvalue()

    except Exception as e:
        logger.error(f"Failed to create voltage chart: {e}")
        return None


def create_battery_chart(hours: int = 24) -> Optional[bytes]:
    """Create battery SOC chart"""
    db = get_db()
    data = db.get_status_history(hours=hours)

    if not data or len(data) < 2:
        return None

    try:
        timestamps = [row['timestamp'] for row in data]
        soc = [row['battery_soc'] or 0 for row in data]

        fig, ax = plt.subplots(figsize=(10, 5))

        ax.plot(timestamps, soc, 'g-', linewidth=2)
        ax.fill_between(timestamps, soc, alpha=0.3, color='green')

        ax.set_xlabel('Час')
        ax.set_ylabel('Заряд (%)')
        ax.set_title(f'Заряд батареї за {hours} годин')
        ax.set_ylim(0, 100)

        ax.axhline(y=20, color='r', linestyle='--', alpha=0.5, label='Критично')
        ax.axhline(y=50, color='orange', linestyle='--', alpha=0.5, label='Низько')

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=max(1, hours // 12)))

        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.legend()
        plt.grid(True, alpha=0.3)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        plt.close(fig)

        return buf.getvalue()

    except Exception as e:
        logger.error(f"Failed to create battery chart: {e}")
        return None


def create_load_chart(hours: int = 24) -> Optional[bytes]:
    """Create load power chart"""
    db = get_db()
    data = db.get_status_history(hours=hours)

    if not data or len(data) < 2:
        return None

    try:
        timestamps = [row['timestamp'] for row in data]
        load = [row['load_power'] or 0 for row in data]

        fig, ax = plt.subplots(figsize=(10, 5))

        ax.plot(timestamps, load, 'orange', linewidth=1)
        ax.fill_between(timestamps, load, alpha=0.3, color='orange')

        ax.set_xlabel('Час')
        ax.set_ylabel('Потужність (W)')
        ax.set_title(f'Навантаження за {hours} годин')

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=max(1, hours // 12)))

        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.grid(True, alpha=0.3)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        plt.close(fig)

        return buf.getvalue()

    except Exception as e:
        logger.error(f"Failed to create load chart: {e}")
        return None


def create_combined_chart(hours: int = 24) -> Optional[bytes]:
    """Create combined chart with voltage, SOC, and load"""
    db = get_db()
    data = db.get_status_history(hours=hours)

    if not data or len(data) < 2:
        return None

    try:
        timestamps = [row['timestamp'] for row in data]
        voltages = [row['grid_voltage'] or 0 for row in data]
        soc = [row['battery_soc'] or 0 for row in data]
        load = [row['load_power'] or 0 for row in data]

        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 10), sharex=True)

        # Voltage
        ax1.plot(timestamps, voltages, 'b-', linewidth=1)
        ax1.fill_between(timestamps, voltages, alpha=0.3)
        ax1.set_ylabel('Напруга (V)')
        ax1.set_title(f'Статистика за {hours} годин')
        ax1.axhline(y=180, color='r', linestyle='--', alpha=0.5)
        ax1.grid(True, alpha=0.3)

        # Mark outages
        for i, row in enumerate(data):
            if not row.get('grid_available', True):
                if i > 0:
                    ax1.axvspan(timestamps[i-1], timestamps[i],
                               alpha=0.3, color='red')

        # SOC
        ax2.plot(timestamps, soc, 'g-', linewidth=2)
        ax2.fill_between(timestamps, soc, alpha=0.3, color='green')
        ax2.set_ylabel('Батарея (%)')
        ax2.set_ylim(0, 100)
        ax2.axhline(y=20, color='r', linestyle='--', alpha=0.5)
        ax2.grid(True, alpha=0.3)

        # Load
        ax3.plot(timestamps, load, 'orange', linewidth=1)
        ax3.fill_between(timestamps, load, alpha=0.3, color='orange')
        ax3.set_ylabel('Навант. (W)')
        ax3.set_xlabel('Час')
        ax3.grid(True, alpha=0.3)

        ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax3.xaxis.set_major_locator(mdates.HourLocator(interval=max(1, hours // 12)))

        plt.xticks(rotation=45)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        plt.close(fig)

        return buf.getvalue()

    except Exception as e:
        logger.error(f"Failed to create combined chart: {e}")
        return None


def create_outage_timeline(hours: int = 24) -> Optional[bytes]:
    """Create outage timeline visualization"""
    db = get_db()
    events = db.get_events(hours=hours)

    grid_events = [e for e in events if e['event_type'] in ('grid_on', 'grid_off')]

    if not grid_events:
        return None

    try:
        fig, ax = plt.subplots(figsize=(10, 3))

        now = datetime.now()
        start = now - timedelta(hours=hours)

        # Draw timeline
        ax.axhline(y=0.5, color='gray', linewidth=2)

        # Mark outages
        grid_off_time = None

        for event in sorted(grid_events, key=lambda x: x['timestamp']):
            ts = event['timestamp']

            if event['event_type'] == 'grid_off':
                grid_off_time = ts
            elif event['event_type'] == 'grid_on' and grid_off_time:
                ax.axvspan(grid_off_time, ts, alpha=0.5, color='red')
                grid_off_time = None

        # If currently off
        if grid_off_time:
            ax.axvspan(grid_off_time, now, alpha=0.5, color='red')

        ax.set_xlim(start, now)
        ax.set_ylim(0, 1)
        ax.set_yticks([])
        ax.set_xlabel('Час')
        ax.set_title(f'Відключення мережі за {hours} годин')

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=max(1, hours // 6)))

        plt.xticks(rotation=45)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        plt.close(fig)

        return buf.getvalue()

    except Exception as e:
        logger.error(f"Failed to create outage timeline: {e}")
        return None
