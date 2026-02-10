"""
Database module for LuxPower Telegram Bot
PostgreSQL storage for inverter data and events
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

import config

logger = logging.getLogger(__name__)


class Database:
    """PostgreSQL database handler"""

    def __init__(self, database_url: str = None):
        self.database_url = database_url or config.DATABASE_URL
        self.pool: Optional[ThreadedConnectionPool] = None

    def connect(self):
        """Initialize connection pool"""
        try:
            self.pool = ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=self.database_url
            )
            logger.info("Database connection pool created")
            self._create_tables()
        except Exception as e:
            logger.error(f"Failed to create connection pool: {e}")
            raise

    def close(self):
        """Close connection pool"""
        if self.pool:
            self.pool.closeall()
            logger.info("Database connection pool closed")

    @contextmanager
    def get_connection(self):
        """Get connection from pool"""
        conn = self.pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.pool.putconn(conn)

    def _create_tables(self):
        """Create tables if not exist"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                # Таблиця статусів
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS inverter_status (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        connected BOOLEAN NOT NULL,
                        grid_available BOOLEAN,
                        grid_voltage REAL,
                        grid_frequency REAL,
                        battery_soc INTEGER,
                        battery_voltage REAL,
                        battery_current REAL,
                        battery_power REAL,
                        load_power INTEGER,
                        output_voltage REAL,
                        output_frequency REAL,
                        inverter_temp INTEGER,
                        radiator_temp REAL,
                        dc_bus_voltage REAL
                    );

                    CREATE INDEX IF NOT EXISTS idx_status_timestamp
                    ON inverter_status(timestamp DESC);
                """)

                # Таблиця подій
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        event_type VARCHAR(50) NOT NULL,
                        data JSONB,
                        notified BOOLEAN DEFAULT FALSE
                    );

                    CREATE INDEX IF NOT EXISTS idx_events_timestamp
                    ON events(timestamp DESC);

                    CREATE INDEX IF NOT EXISTS idx_events_type
                    ON events(event_type);
                """)

                # Таблиця підписників
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS subscribers (
                        id SERIAL PRIMARY KEY,
                        chat_id BIGINT UNIQUE NOT NULL,
                        username VARCHAR(255),
                        subscribed_at TIMESTAMPTZ DEFAULT NOW(),
                        is_active BOOLEAN DEFAULT TRUE
                    );
                """)

                logger.info("Database tables created/verified")

    # =========================================================================
    # STATUS METHODS
    # =========================================================================

    def save_status(self, status: Dict[str, Any]):
        """Save inverter status to database"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO inverter_status (
                        timestamp, connected, grid_available, grid_voltage,
                        grid_frequency, battery_soc, battery_voltage,
                        battery_current, battery_power, load_power,
                        output_voltage, output_frequency, inverter_temp,
                        radiator_temp, dc_bus_voltage
                    ) VALUES (
                        to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                """, (
                    status.get('timestamp'),
                    status.get('connected', False),
                    status.get('grid', {}).get('available'),
                    status.get('grid', {}).get('voltage'),
                    status.get('grid', {}).get('frequency'),
                    status.get('battery', {}).get('soc'),
                    status.get('battery', {}).get('voltage'),
                    status.get('battery', {}).get('current'),
                    status.get('battery', {}).get('power'),
                    status.get('output', {}).get('load_power'),
                    status.get('output', {}).get('voltage'),
                    status.get('output', {}).get('frequency'),
                    status.get('temperature', {}).get('inverter'),
                    status.get('temperature', {}).get('radiator'),
                    status.get('dc_bus_voltage')
                ))
                logger.debug("Status saved to database")

    def get_latest_status(self) -> Optional[Dict]:
        """Get latest status from database"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM inverter_status
                    ORDER BY timestamp DESC
                    LIMIT 1
                """)
                row = cur.fetchone()
                return dict(row) if row else None

    def get_status_history(self, hours: int = 24) -> List[Dict]:
        """Get status history for specified hours"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM inverter_status
                    WHERE timestamp > NOW() - INTERVAL '%s hours'
                    ORDER BY timestamp ASC
                """, (hours,))
                return [dict(row) for row in cur.fetchall()]

    def get_hourly_averages(self, hours: int = 24) -> List[Dict]:
        """Get hourly averages for charts"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        date_trunc('hour', timestamp) as hour,
                        AVG(grid_voltage) as avg_grid_voltage,
                        AVG(battery_soc) as avg_battery_soc,
                        AVG(load_power) as avg_load_power,
                        BOOL_AND(grid_available) as grid_was_available
                    FROM inverter_status
                    WHERE timestamp > NOW() - INTERVAL '%s hours'
                    GROUP BY date_trunc('hour', timestamp)
                    ORDER BY hour ASC
                """, (hours,))
                return [dict(row) for row in cur.fetchall()]

    # =========================================================================
    # EVENTS METHODS
    # =========================================================================

    def save_event(self, event_type: str, data: Dict = None):
        """Save event to database"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO events (event_type, data)
                    VALUES (%s, %s)
                    RETURNING id
                """, (event_type, psycopg2.extras.Json(data or {})))
                event_id = cur.fetchone()[0]
                logger.info(f"Event saved: {event_type} (id={event_id})")
                return event_id

    def get_events(self, event_type: str = None, hours: int = 24,
                   limit: int = 100) -> List[Dict]:
        """Get events from database"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if event_type:
                    cur.execute("""
                        SELECT * FROM events
                        WHERE event_type = %s
                        AND timestamp > NOW() - INTERVAL '%s hours'
                        ORDER BY timestamp DESC
                        LIMIT %s
                    """, (event_type, hours, limit))
                else:
                    cur.execute("""
                        SELECT * FROM events
                        WHERE timestamp > NOW() - INTERVAL '%s hours'
                        ORDER BY timestamp DESC
                        LIMIT %s
                    """, (hours, limit))
                return [dict(row) for row in cur.fetchall()]

    def get_grid_statistics(self, hours: int = 24) -> Dict:
        """Calculate grid outage statistics"""
        events = self.get_events(hours=hours)

        grid_events = [e for e in events if e['event_type'] in ('grid_on', 'grid_off')]

        outage_count = 0
        total_duration = 0

        for event in grid_events:
            if event['event_type'] == 'grid_on':
                outage_count += 1
                duration = event.get('data', {}).get('duration_seconds', 0)
                if duration:
                    total_duration += duration

        return {
            'period_hours': hours,
            'outage_count': outage_count,
            'total_duration_seconds': total_duration,
            'total_duration_minutes': round(total_duration / 60, 1)
        }

    # =========================================================================
    # SUBSCRIBERS METHODS
    # =========================================================================

    def add_subscriber(self, chat_id: int, username: str = None) -> bool:
        """Add or reactivate subscriber"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO subscribers (chat_id, username, is_active)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (chat_id) DO UPDATE
                    SET is_active = TRUE, username = EXCLUDED.username
                    RETURNING id
                """, (chat_id, username))
                return cur.fetchone() is not None

    def remove_subscriber(self, chat_id: int) -> bool:
        """Deactivate subscriber"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE subscribers SET is_active = FALSE
                    WHERE chat_id = %s
                """, (chat_id,))
                return cur.rowcount > 0

    def get_active_subscribers(self) -> List[int]:
        """Get list of active subscriber chat IDs"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT chat_id FROM subscribers
                    WHERE is_active = TRUE
                """)
                return [row[0] for row in cur.fetchall()]

    def is_subscribed(self, chat_id: int) -> bool:
        """Check if chat is subscribed"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT is_active FROM subscribers
                    WHERE chat_id = %s
                """, (chat_id,))
                row = cur.fetchone()
                return row[0] if row else False

    # =========================================================================
    # CLEANUP METHODS
    # =========================================================================

    def cleanup_old_data(self, days: int = 30):
        """Remove data older than specified days"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM inverter_status
                    WHERE timestamp < NOW() - INTERVAL '%s days'
                """, (days,))
                deleted_status = cur.rowcount

                cur.execute("""
                    DELETE FROM events
                    WHERE timestamp < NOW() - INTERVAL '%s days'
                """, (days,))
                deleted_events = cur.rowcount

                logger.info(f"Cleanup: removed {deleted_status} status records, "
                           f"{deleted_events} events")


# Global instance
_db: Optional[Database] = None


def get_db() -> Database:
    """Get database instance"""
    global _db
    if _db is None:
        _db = Database()
        _db.connect()
    return _db


def close_db():
    """Close database connection"""
    global _db
    if _db:
        _db.close()
        _db = None
