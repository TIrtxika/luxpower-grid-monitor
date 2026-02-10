#!/usr/bin/env python3
"""
LuxPower REST API Service
Надає доступ до даних інвертора через HTTP API
"""

import json
import time
import logging
from pathlib import Path
from functools import wraps
from flask import Flask, jsonify, request, Response

import config

# Налаштування логування
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("api")

app = Flask(__name__)

# Шляхи до файлів кешу (спільні з collector)
CACHE_FILE = Path("/tmp/luxpower_status.json")
EVENTS_FILE = Path("/tmp/luxpower_events.json")


def require_auth(f):
    """Декоратор для перевірки авторизації"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing authorization header"}), 401

        token = auth_header[7:]  # Видаляємо "Bearer "

        if token != config.API_TOKEN:
            return jsonify({"error": "Invalid token"}), 403

        return f(*args, **kwargs)
    return decorated


def read_cache_file(filepath: Path) -> dict:
    """Читання JSON файлу"""
    try:
        if filepath.exists():
            with open(filepath, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error reading {filepath}: {e}")
    return {}


def read_events_file() -> list:
    """Читання файлу подій"""
    try:
        if EVENTS_FILE.exists():
            with open(EVENTS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error reading events: {e}")
    return []


# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint (без авторизації)"""
    status = read_cache_file(CACHE_FILE)
    is_fresh = False

    if status.get('timestamp'):
        age = time.time() - status['timestamp']
        is_fresh = age < config.POLL_INTERVAL * 3

    return jsonify({
        "status": "ok" if is_fresh else "stale",
        "timestamp": time.time(),
        "data_age_seconds": int(time.time() - status.get('timestamp', 0)) if status.get('timestamp') else None
    })


@app.route('/status', methods=['GET'])
@require_auth
def get_status():
    """Отримання поточного статусу інвертора"""
    status = read_cache_file(CACHE_FILE)

    if not status:
        return jsonify({
            "error": "No data available",
            "message": "Collector may not be running"
        }), 503

    # Додаємо інформацію про свіжість даних
    if status.get('timestamp'):
        status['data_age_seconds'] = int(time.time() - status['timestamp'])

    return jsonify(status)


@app.route('/status/simple', methods=['GET'])
@require_auth
def get_status_simple():
    """Спрощений статус для public бота"""
    status = read_cache_file(CACHE_FILE)

    if not status:
        return jsonify({"error": "No data available"}), 503

    grid = status.get('grid', {})
    battery = status.get('battery', {})

    return jsonify({
        "timestamp": status.get('timestamp'),
        "connected": status.get('connected', False),
        "grid_available": grid.get('available', False),
        "grid_voltage": grid.get('voltage', 0),
        "battery_soc": battery.get('soc', 0)
    })


@app.route('/events', methods=['GET'])
@require_auth
def get_events():
    """Отримання подій"""
    events = read_events_file()

    # Фільтрація за типом
    event_type = request.args.get('type')
    if event_type:
        events = [e for e in events if e.get('type') == event_type]

    # Фільтрація за часом
    since = request.args.get('since')
    if since:
        try:
            since_ts = float(since)
            events = [e for e in events if e.get('timestamp', 0) >= since_ts]
        except ValueError:
            pass

    # Ліміт
    limit = request.args.get('limit', 100, type=int)
    events = events[-limit:]

    return jsonify({
        "count": len(events),
        "events": events
    })


@app.route('/events/grid', methods=['GET'])
@require_auth
def get_grid_events():
    """Отримання подій мережі (grid_on, grid_off)"""
    events = read_events_file()

    grid_events = [
        e for e in events
        if e.get('type') in ('grid_on', 'grid_off')
    ]

    # Фільтрація за останні N годин
    hours = request.args.get('hours', 24, type=int)
    since_ts = time.time() - (hours * 3600)
    grid_events = [e for e in grid_events if e.get('timestamp', 0) >= since_ts]

    # Обчислюємо статистику
    total_off_duration = 0
    outage_count = 0

    for event in grid_events:
        if event.get('type') == 'grid_on':
            duration = event.get('data', {}).get('duration_seconds')
            if duration:
                total_off_duration += duration
                outage_count += 1

    return jsonify({
        "period_hours": hours,
        "outage_count": outage_count,
        "total_off_duration_seconds": total_off_duration,
        "total_off_duration_minutes": round(total_off_duration / 60, 1),
        "events": grid_events
    })


@app.route('/stats', methods=['GET'])
@require_auth
def get_stats():
    """Статистика за період"""
    status = read_cache_file(CACHE_FILE)
    events = read_events_file()

    hours = request.args.get('hours', 24, type=int)
    since_ts = time.time() - (hours * 3600)

    grid_events = [
        e for e in events
        if e.get('type') in ('grid_on', 'grid_off') and e.get('timestamp', 0) >= since_ts
    ]

    # Підрахунок відключень
    total_off_duration = 0
    outages = []

    for event in grid_events:
        if event.get('type') == 'grid_on':
            duration = event.get('data', {}).get('duration_seconds')
            if duration:
                total_off_duration += duration
                outages.append({
                    "timestamp": event.get('timestamp'),
                    "duration_seconds": duration
                })

    return jsonify({
        "period_hours": hours,
        "current": {
            "connected": status.get('connected', False),
            "grid_available": status.get('grid', {}).get('available', False),
            "grid_voltage": status.get('grid', {}).get('voltage', 0),
            "battery_soc": status.get('battery', {}).get('soc', 0),
            "load_power": status.get('output', {}).get('load_power', 0)
        },
        "outages": {
            "count": len(outages),
            "total_duration_seconds": total_off_duration,
            "total_duration_minutes": round(total_off_duration / 60, 1),
            "list": outages[-10:]  # Останні 10
        }
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info(f"Starting API server on {config.API_HOST}:{config.API_PORT}")

    # Для production використовуйте gunicorn:
    # gunicorn -w 2 -b 0.0.0.0:8080 api:app

    app.run(
        host=config.API_HOST,
        port=config.API_PORT,
        debug=False,
        threaded=True
    )


if __name__ == "__main__":
    main()
