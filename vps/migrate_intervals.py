#!/usr/bin/env python3
"""
One-off backfill: build grid_intervals from inverter_status samples

Run with the public bot stopped:
    python migrate_intervals.py --dry-run   # show what would be written
    python migrate_intervals.py             # write (refuses if table not empty)
    python migrate_intervals.py --force     # wipe grid_intervals and rewrite
"""

import argparse
from datetime import timedelta
from typing import Dict, List, Optional, Sequence

# Larger gaps between 5-minute samples mean we did not observe the grid
MAX_SAMPLE_GAP = timedelta(minutes=10)


def sample_state(sample: Dict) -> str:
    if not sample.get('connected') or sample.get('grid_available') is None:
        return 'unknown'
    return 'on' if sample['grid_available'] else 'off'


def samples_to_intervals(samples: Sequence[Dict],
                         max_gap: timedelta = MAX_SAMPLE_GAP) -> List[Dict]:
    """Samples (oldest first) -> closed intervals; a change is placed at the
    first sample showing the new state"""
    intervals: List[Dict] = []
    current: Optional[Dict] = None
    prev_ts = None

    for sample in samples:
        ts = sample['timestamp']
        state = sample_state(sample)

        if current is not None and ts - prev_ts > max_gap:
            current['ended_at'] = prev_ts
            intervals.append(current)
            intervals.append({'state': 'unknown', 'started_at': prev_ts,
                              'ended_at': ts})
            current = None

        if current is None:
            current = {'state': state, 'started_at': ts}
        elif state != current['state']:
            current['ended_at'] = ts
            intervals.append(current)
            current = {'state': state, 'started_at': ts}
        prev_ts = ts

    if current is not None:
        current['ended_at'] = prev_ts
        intervals.append(current)

    # Merge neighbours with the same state (e.g. gap next to unknown samples)
    merged: List[Dict] = []
    for iv in intervals:
        if (merged and merged[-1]['state'] == iv['state']
                and merged[-1]['ended_at'] == iv['started_at']):
            merged[-1]['ended_at'] = iv['ended_at']
        else:
            merged.append(dict(iv))
    return [iv for iv in merged if iv['ended_at'] > iv['started_at']]


def main(argv=None, db=None) -> int:
    parser = argparse.ArgumentParser(description="Backfill grid_intervals")
    parser.add_argument('--dry-run', action='store_true',
                        help="print the result without writing")
    parser.add_argument('--force', action='store_true',
                        help="wipe grid_intervals before writing")
    args = parser.parse_args(argv)

    if db is None:
        from database import get_db
        db = get_db()

    existing = db.count_intervals()
    if existing and not (args.force or args.dry_run):
        print(f"grid_intervals already has {existing} rows; "
              f"use --force to rewrite (stop the bot first)")
        return 1

    samples = db.get_state_samples()
    intervals = samples_to_intervals(samples)

    totals = {'on': 0.0, 'off': 0.0, 'unknown': 0.0}
    for iv in intervals:
        totals[iv['state']] += (iv['ended_at'] - iv['started_at']).total_seconds()

    print(f"samples: {len(samples)}, intervals: {len(intervals)}")
    for state, seconds in totals.items():
        print(f"  {state}: {seconds / 3600:.1f} h")

    if args.dry_run:
        return 0

    if args.force:
        db.truncate_intervals()
    db.insert_intervals(intervals)
    print(f"written: {len(intervals)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
