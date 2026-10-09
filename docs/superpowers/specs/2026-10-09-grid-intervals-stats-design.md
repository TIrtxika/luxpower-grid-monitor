# Grid intervals, real statistics and bot improvements — design

Date: 2026-10-09
Status: Stage 1 designed in detail; stages 2–4 outlined (each gets its own detailed design before implementation).

## Goal

The owner wants the LuxPower grid monitor bot to:

- show a command menu and a traffic-light status (🟢 grid on, 🔴 grid off, 🟡 unknown / no fresh data);
- have correct, readable charts (styled matplotlib, Kyiv time, honest gaps);
- give real statistics: availability per Kyiv day/week/month, an outage list (including the ongoing one), and an hour-of-day pattern (day × hour heatmap);
- notify the owner outside Telegram via ntfy (ntfy.sh, secret topic), because the owner is not always in Telegram.

Success: numbers in `/grid`, `/history`, `/stats` and `/status` match reality, including periods when the system did not know the state (e.g. VPS down 2026-09-28 – 2026-10-08), which must show as "unknown" rather than being guessed.

## Current state (verified in code, 2026-10-09)

- Grid state is binary; no unknown state. The poller ignores `connected` and `data_age_seconds` from the RPi (`vps/poller.py:199-216`), so stale data is treated as live.
- `/grid` extrapolates the share of samples to the whole period (`vps/bot.py:306`); days with no data silently disappear; "Рік" button is 6 months.
- `/stats` and `/history` count only finished outages from `grid_on` events (`vps/database.py:237-258`); ongoing outage not counted; outages > 24 h get duration 0.
- History detail shows the oldest 10 events: `get_events` is `ORDER BY timestamp DESC` (`vps/database.py:225`, `:232`) and the handler takes `[-10:]` (`vps/bot.py:202`).
- Owner detail alerts are dead code: `AlertManager(app.bot)` (`vps/bot.py:564`) leaves `private_bot=None` (`vps/alerts.py:32`, checked at `:154`).
- httpx logs full Telegram URLs with bot tokens at INFO; no logger override.
- Charts: axis labels in UTC, gaps drawn as straight lines, NULL drawn as 0, sync rendering inside async handlers.
- No `set_my_commands`; only inline buttons.

## Stage 1 — state intervals and real statistics (detailed)

### 1.1 Effective state

Computed on every poll in `RpiPoller._poll_once`:

- `unknown` if any of:
  - RPi fetch failed `RPI_UNREACHABLE_THRESHOLD` times in a row (default 3); the unknown interval starts at the time of the **first** failure;
  - RPi responded with `connected == false` (inverter dongle offline);
  - `data_age_seconds > STALE_DATA_SECONDS` (new config, default 180);
  - `grid.available is None`.
- `on` / `off` from `grid.available`, with the existing VPS debounce (`GRID_STATE_DEBOUNCE`, 120 s). The interval boundary is the moment the change was **first observed** (`_pending_since`), not the moment it was confirmed.
- Leaving `unknown` is accepted immediately (no debounce): the RPi already debounces locally.
- All interval boundaries use the VPS clock (single time source).

### 1.2 Table `grid_intervals`

```sql
CREATE TABLE IF NOT EXISTS grid_intervals (
    id SERIAL PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('on', 'off', 'unknown')),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NULL,          -- NULL = current interval
    last_seen_at TIMESTAMPTZ NOT NULL   -- heartbeat
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_grid_intervals_open
    ON grid_intervals ((ended_at IS NULL)) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_grid_intervals_started
    ON grid_intervals (started_at);
```

Maintenance:

- Each poll: same state → update `last_seen_at` of the open interval; different state → close the open interval at the boundary time and open a new one, in one transaction.
- Startup gap detection: if the open interval's `last_seen_at` is older than `3 × POLL_INTERVAL`, close it at `last_seen_at` and insert `unknown` from `last_seen_at` to now. This records VPS/bot downtime honestly.
- DB failure: the poller keeps the state in memory, logs, and retries on the next poll; the open interval simply extends.

Tables `events` and `inverter_status` stay: events for the journal and alerts, samples for voltage/SOC/load charts.

### 1.3 Backfill migration

One-off script `vps/migrate_intervals.py`:

- reads `inverter_status` ordered by time (5-minute samples);
- merges consecutive samples with the same `grid_available` into intervals;
- a gap > 10 minutes between samples, or `connected = false`, or NULL `grid_available`, becomes `unknown`;
- `--dry-run` prints the interval count and the totals per state without writing;
- refuses to run if `grid_intervals` already has rows (idempotency guard), unless `--force`.

### 1.4 Statistics module `vps/stats.py`

SQL only selects intervals overlapping a window:

```sql
SELECT state, started_at, COALESCE(ended_at, NOW()) AS ended_at, ended_at IS NULL AS ongoing
FROM grid_intervals
WHERE started_at < %(to)s AND COALESCE(ended_at, NOW()) > %(from)s
ORDER BY started_at
```

Pure functions over that list (no DB, unit-testable); period boundaries via `ZoneInfo("Europe/Kyiv")` so 23 h / 25 h DST days work:

- `split_by_periods(intervals, period, count, now)` — `period` in `day | week | month`; week = Monday–Sunday in Kyiv; returns per period the seconds of `on`, `off`, `unknown`; the current period is clipped at `now` and flagged partial.
- `availability_pct(on, off)` = `on / (on + off)`; unknown time is reported separately and never folded into the percentage. Returns None if `on + off == 0`.
- `outages(intervals, start, end)` — `off` intervals, newest first, including the ongoing one (`ongoing=True`); if an outage touches an `unknown` interval on either side, its duration is marked as a lower bound ("≥ X").
- `outage_summary(outages)` — count, total, average, longest.
- `hourly_heatmap(intervals, days, now)` — matrix day × hour (Kyiv) with the share of `off` and `unknown` per cell; data only, drawing is stage 3.

### 1.5 Command changes in stage 1

- `/grid` — availability per day / week / month from `split_by_periods`; the "Рік" button shows 12 months.
- `/history` — outage list from `outages`, newest first, ongoing included (fixes the oldest-10 bug).
- `/stats` (owner) — `outage_summary` + availability for 24 h, 7 d, 30 d.
- `/status` — "з HH:MM (тривалість)" from the open interval.
- Remove `get_grid_statistics` and the extrapolation at `vps/bot.py:306`.

### 1.6 Fixes included in stage 1

1. Set `httpx` and `httpcore` loggers to WARNING (stop leaking bot tokens).
2. `AlertManager._run_async`: use `asyncio.run_coroutine_threadsafe` instead of `ensure_future` from the poller thread.
3. Save the event **before** fan-out, so a sending failure cannot lose it; the "grid back" alert takes the outage duration from the closed `off` interval.
4. On `Forbidden` (bot blocked by the user) deactivate the subscriber.
5. Owner detail alert: construct `AlertManager` with `Bot(PRIVATE_BOT_TOKEN)` as `private_bot`.
6. 🟡 alerts to the owner: when `unknown` lasts more than `UNKNOWN_ALERT_AFTER` (default 300 s) send "немає даних: <reason>" (RPi unreachable / dongle offline / stale data); send "дані відновились" when it ends. Subscribers get a normal on/off alert after `unknown` only if the state actually changed, with a note that the change time is approximate.
7. Retention: once a day the poller deletes `inverter_status` rows older than `RETENTION_DAYS` (default 90). Intervals and events are kept.

Out of scope for stage 1: RPi events/cache in `/tmp` (VPS now detects staleness itself); blocking `requests` in the private bot (its `/status` moves to DB data in stage 2).

### 1.7 Testing

`unittest`, same style as `vps/test_timezone.py`:

- `stats.py`: DST transition day, outage across midnight, ongoing outage, `unknown` in the middle, partial current day, empty window.
- Poller state machine: mocked `fetch_status` and an injected clock; unreachable threshold (start at first failure), `connected=false`, stale data, debounce with boundary at `_pending_since`, leaving unknown without debounce.
- DB layer: integration tests against a real Postgres, run only when `TEST_DATABASE_URL` is set (skipped otherwise): open/close in one transaction, single open interval, startup gap → unknown.
- Migration: `--dry-run` on a copy of production data before the real run; the real run only with the owner's approval.

### 1.8 Deployment

Backup `/opt/luxpower` and the DB (`pg_dump`), deploy current `main` to the VPS, run the migration, restart both bots, check `/status`, `/grid`, `/history`. Every production step needs the owner's confirmation. Note: the VPS currently runs code older than `main` plus a hot patch (Kyiv timezone, backups `*.bak-20261009`).

## Stage 2 — menu and traffic-light status (outline)

- `set_my_commands` for the public and the private bot (separate lists); a persistent reply keyboard with the main commands.
- Traffic light from the effective state: 🟢 on, 🔴 off, 🟡 unknown with the reason and the age of the last data.
- Private bot `/status` reads DB data (open interval + latest sample) instead of a blocking `requests` call.

## Stage 3 — charts (outline)

- Styled matplotlib theme (consistent colours for on/off/unknown, hi-DPI, readable fonts), all axes in Kyiv time.
- Gaps rendered as gaps (NaN breaks), NULL never drawn as 0.
- Timeline from `grid_intervals` (works on a day without outages); heatmap from `hourly_heatmap`.
- Rendering off the event loop (`asyncio.to_thread`) with the object-oriented matplotlib API (no pyplot global state).

## Stage 4 — ntfy notifications (outline)

- A `Notifier` interface (`send(title, text, priority, tags)`) with `TelegramNotifier` and `NtfyNotifier`; `AlertManager` fans out to the registered notifiers.
- ntfy.sh with a secret topic from `.env` (`NTFY_URL`, `NTFY_TOPIC`, optional `NTFY_TOKEN`); owner only.
- Priorities: grid off → high, grid back → default, 🟡 unknown → default, data restored → low.
