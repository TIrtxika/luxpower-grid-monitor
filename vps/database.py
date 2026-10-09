"""
Database module for LuxPower Telegram Bot
PostgreSQL storage for inverter data and events
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from contextlib import contextmanager

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

import config
from stats import Interval
from schedule import DaySchedule, day_from_dict, day_to_dict
from subscriptions import Settings

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

                # Налаштування сповіщень підписника (тихі години, режим)
                cur.execute("""
                    ALTER TABLE subscribers
                        ADD COLUMN IF NOT EXISTS quiet_enabled BOOLEAN
                            NOT NULL DEFAULT TRUE,
                        ADD COLUMN IF NOT EXISTS quiet_from TIME
                            NOT NULL DEFAULT '23:00',
                        ADD COLUMN IF NOT EXISTS quiet_to TIME
                            NOT NULL DEFAULT '07:00',
                        ADD COLUMN IF NOT EXISTS notify_mode TEXT
                            NOT NULL DEFAULT 'all'
                            CHECK (notify_mode IN ('all', 'off_only', 'on_only')),
                        ADD COLUMN IF NOT EXISTS remind_enabled BOOLEAN
                            NOT NULL DEFAULT TRUE;
                """)

                # Графік планових відключень групи (YASNO), по днях
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS planned_outages (
                        day DATE NOT NULL,
                        grp TEXT NOT NULL,
                        status TEXT NOT NULL,
                        outages JSONB NOT NULL,
                        fetched_at TIMESTAMPTZ NOT NULL,
                        PRIMARY KEY (day, grp)
                    );
                """)

                # Таблиця інтервалів стану мережі
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS grid_intervals (
                        id SERIAL PRIMARY KEY,
                        state TEXT NOT NULL
                            CHECK (state IN ('on', 'off', 'unknown')),
                        started_at TIMESTAMPTZ NOT NULL,
                        ended_at TIMESTAMPTZ NULL,
                        last_seen_at TIMESTAMPTZ NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_grid_intervals_open
                    ON grid_intervals ((ended_at IS NULL)) WHERE ended_at IS NULL;

                    CREATE INDEX IF NOT EXISTS idx_grid_intervals_started
                    ON grid_intervals (started_at);
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
    # GRID INTERVALS
    # =========================================================================

    def get_open_interval(self) -> Optional[Dict]:
        """Current (open) grid state interval"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT state, started_at, last_seen_at FROM grid_intervals
                    WHERE ended_at IS NULL
                """)
                row = cur.fetchone()
                return dict(row) if row else None

    def get_last_known_state(self) -> Optional[str]:
        """Latest 'on'/'off' state, ignoring unknown intervals"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT state FROM grid_intervals
                    WHERE state <> 'unknown'
                    ORDER BY started_at DESC
                    LIMIT 1
                """)
                row = cur.fetchone()
                return row[0] if row else None

    def heartbeat(self, now: datetime) -> int:
        """Mark the open interval as still observed; 0 = no open interval"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE grid_intervals SET last_seen_at = %s
                    WHERE ended_at IS NULL
                """, (now,))
                return cur.rowcount

    def switch_state(self, state: str, at: datetime,
                     now: datetime) -> Optional[Dict]:
        """Close the open interval at `at` and open a new one in `state`.

        Returns the closed interval (state, started_at, ended_at) or None.
        """
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, state, started_at FROM grid_intervals
                    WHERE ended_at IS NULL
                    FOR UPDATE
                """)
                row = cur.fetchone()

                if row and row['state'] == state:
                    cur.execute("""
                        UPDATE grid_intervals SET last_seen_at = %s
                        WHERE id = %s
                    """, (now, row['id']))
                    return None

                closed = None
                boundary = at
                if row:
                    # Boundary never goes before the start of the open interval
                    boundary = max(at, row['started_at'])
                    cur.execute("""
                        UPDATE grid_intervals
                        SET ended_at = %s,
                            last_seen_at = GREATEST(last_seen_at, %s)
                        WHERE id = %s
                    """, (boundary, boundary, row['id']))
                    closed = {'state': row['state'],
                              'started_at': row['started_at'],
                              'ended_at': boundary}

                cur.execute("""
                    INSERT INTO grid_intervals (state, started_at, last_seen_at)
                    VALUES (%s, %s, %s)
                """, (state, boundary, max(now, boundary)))
                return closed

    def recover_gap(self, now: datetime,
                    max_gap: timedelta) -> Optional[Tuple[datetime, datetime]]:
        """Record monitoring downtime as unknown.

        If the open interval was last seen more than `max_gap` ago, close it
        at last_seen_at and open 'unknown' from there. Returns (start, now).
        """
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, state, last_seen_at FROM grid_intervals
                    WHERE ended_at IS NULL
                    FOR UPDATE
                """)
                row = cur.fetchone()
                if not row or now - row['last_seen_at'] <= max_gap:
                    return None

                gap_start = row['last_seen_at']
                if row['state'] == 'unknown':
                    cur.execute("""
                        UPDATE grid_intervals SET last_seen_at = %s
                        WHERE id = %s
                    """, (now, row['id']))
                else:
                    cur.execute("""
                        UPDATE grid_intervals SET ended_at = last_seen_at
                        WHERE id = %s
                    """, (row['id'],))
                    cur.execute("""
                        INSERT INTO grid_intervals (state, started_at, last_seen_at)
                        VALUES ('unknown', %s, %s)
                    """, (gap_start, now))
                return gap_start, now

    def get_intervals(self, start: datetime, end: datetime) -> List[Interval]:
        """Intervals overlapping [start, end), plus the one before the first of
        them (so an outage next to unknown time is marked as a lower bound).

        The open interval ends at NOW(), but never later than its last
        heartbeat + 3 polls: if the monitor stopped, that time is unknown.
        """
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    WITH iv AS (
                        SELECT id, state, started_at,
                               COALESCE(ended_at, LEAST(
                                   NOW(),
                                   last_seen_at + make_interval(secs => %(gap)s)
                               )) AS ended_at,
                               ended_at IS NULL AS ongoing
                        FROM grid_intervals
                    )
                    SELECT state, started_at, ended_at, ongoing
                    FROM iv
                    WHERE (started_at < %(end)s AND ended_at > %(start)s)
                       OR id IN (SELECT id FROM grid_intervals
                                 WHERE started_at < %(start)s
                                 ORDER BY started_at DESC LIMIT 2)
                    ORDER BY started_at
                """, {'start': start, 'end': end,
                      'gap': 3 * config.POLL_INTERVAL})
                return [Interval(row['state'], row['started_at'],
                                 row['ended_at'], row['ongoing'])
                        for row in cur.fetchall()]

    def count_intervals(self) -> int:
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM grid_intervals")
                return cur.fetchone()[0]

    def get_state_samples(self) -> List[Dict]:
        """All stored samples (time, connected, grid flag), oldest first"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT timestamp, connected, grid_available
                    FROM inverter_status
                    ORDER BY timestamp
                """)
                return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _insert_closed(cur, rows: List[Dict]):
        cur.executemany("""
            INSERT INTO grid_intervals
                (state, started_at, ended_at, last_seen_at)
            VALUES (%s, %s, %s, %s)
        """, [(r['state'], r['started_at'], r['ended_at'], r['ended_at'])
              for r in rows])

    def insert_intervals(self, rows: List[Dict]):
        """Insert closed intervals (migration)"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                self._insert_closed(cur, rows)

    def replace_intervals(self, rows: List[Dict]):
        """Wipe grid_intervals and insert `rows` in one transaction"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE grid_intervals RESTART IDENTITY")
                self._insert_closed(cur, rows)

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

    _SETTINGS_FIELDS = ('quiet_enabled', 'quiet_from', 'quiet_to', 'notify_mode',
                        'remind_enabled')

    def _settings_from_row(self, row) -> Settings:
        return Settings(chat_id=row['chat_id'], quiet_enabled=row['quiet_enabled'],
                        quiet_from=row['quiet_from'], quiet_to=row['quiet_to'],
                        notify_mode=row['notify_mode'],
                        remind_enabled=row['remind_enabled'])

    def get_settings(self, chat_id: int) -> Optional[Settings]:
        """Notification settings of an active subscriber"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT chat_id, quiet_enabled, quiet_from, quiet_to, notify_mode,
                           remind_enabled
                    FROM subscribers
                    WHERE chat_id = %s AND is_active = TRUE
                """, (chat_id,))
                row = cur.fetchone()
                return self._settings_from_row(row) if row else None

    def get_subscriber_settings(self) -> List[Settings]:
        """Settings of all active subscribers (alert fan-out)"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT chat_id, quiet_enabled, quiet_from, quiet_to, notify_mode,
                           remind_enabled
                    FROM subscribers
                    WHERE is_active = TRUE
                    ORDER BY id
                """)
                return [self._settings_from_row(r) for r in cur.fetchall()]

    def update_settings(self, chat_id: int, **fields) -> bool:
        """Change notification settings; only known fields are accepted"""
        unknown = set(fields) - set(self._SETTINGS_FIELDS)
        if unknown or not fields:
            raise ValueError(f"Unknown settings fields: {sorted(unknown)}")
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(name)) for name in fields)
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("UPDATE subscribers SET {} WHERE chat_id = %s").format(
                        assignments),
                    (*fields.values(), chat_id))
                return cur.rowcount > 0

    # =========================================================================
    # PLANNED OUTAGES (schedule)
    # =========================================================================

    def save_schedule(self, group: str, days: List[DaySchedule],
                      fetched_at: datetime) -> None:
        """Upsert the schedule of a group, one row per day"""
        if not days:
            return
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                for d in days:
                    cur.execute("""
                        INSERT INTO planned_outages (day, grp, status, outages, fetched_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (day, grp) DO UPDATE
                        SET status = EXCLUDED.status, outages = EXCLUDED.outages,
                            fetched_at = EXCLUDED.fetched_at
                    """, (d.day, group, d.status,
                          Json(day_to_dict(d)['outages']), fetched_at))

    def get_schedule(self, group: str, from_day: date,
                     to_day: date) -> List[DaySchedule]:
        """Stored schedule of a group for days in [from_day, to_day]"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT day, status, outages FROM planned_outages
                    WHERE grp = %s AND day BETWEEN %s AND %s
                    ORDER BY day
                """, (group, from_day, to_day))
                return [day_from_dict({'day': day.isoformat(), 'status': status,
                                       'outages': outages})
                        for day, status, outages in cur.fetchall()]

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

    def cleanup_status(self, days: int) -> int:
        """Remove inverter_status samples older than `days`"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM inverter_status
                    WHERE timestamp < NOW() - make_interval(days => %s)
                """, (days,))
                return cur.rowcount


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
