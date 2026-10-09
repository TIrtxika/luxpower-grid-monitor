"""
Planned outage schedule (YASNO): parsing, status line, reminders, plan totals.
Run: python -m unittest test_schedule
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from schedule import (
    DaySchedule, PlannedOutage, day_from_dict, day_to_dict, describe,
    due_reminders, fetch_group, fetch_schedule, parse_group, planned_seconds,
)
from stats import KYIV_TZ


def K(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=KYIV_TZ)


def slots(*spans, kind='Definite'):
    out, cursor = [], 0
    for a, b in spans:
        if a > cursor:
            out.append({'start': cursor, 'end': a, 'type': 'NotPlanned'})
        out.append({'start': a, 'end': b, 'type': kind})
        cursor = b
    if cursor < 1440:
        out.append({'start': cursor, 'end': 1440, 'type': 'NotPlanned'})
    return out


PAYLOAD = {
    '16.1': {
        'today': {'slots': slots((540, 750), (780, 900), (1080, 1320)),
                  'date': '2026-10-09T00:00:00+03:00', 'status': 'ScheduleApplies'},
        'tomorrow': {'slots': slots((720, 930)),
                     'date': '2026-10-10T00:00:00+03:00', 'status': 'ScheduleApplies'},
        'updatedOn': '2026-10-09T18:45:02+00:00',
    },
    '1.1': {'today': {'slots': [], 'date': '2026-10-09T00:00:00+03:00',
                      'status': 'NoOutages'}},
}


def days(today=('ScheduleApplies', [(K(2026, 10, 9, 9), K(2026, 10, 9, 12, 30))]),
         tomorrow=('ScheduleApplies', [])):
    out = []
    for d, (status, spans) in ((date(2026, 10, 9), today), (date(2026, 10, 10), tomorrow)):
        out.append(DaySchedule(d, status, tuple(PlannedOutage(a, b) for a, b in spans)))
    return out


class ParseTest(unittest.TestCase):
    def test_parse_group(self):
        today, tomorrow = parse_group(PAYLOAD, '16.1')
        self.assertEqual((today.day, today.status), (date(2026, 10, 9), 'ScheduleApplies'))
        self.assertEqual(today.outages, (
            PlannedOutage(K(2026, 10, 9, 9), K(2026, 10, 9, 12, 30)),
            PlannedOutage(K(2026, 10, 9, 13), K(2026, 10, 9, 15)),
            PlannedOutage(K(2026, 10, 9, 18), K(2026, 10, 9, 22)),
        ))
        self.assertEqual(tomorrow.outages,
                         (PlannedOutage(K(2026, 10, 10, 12), K(2026, 10, 10, 15, 30)),))

    def test_adjacent_slots_are_merged_and_end_of_day_is_next_midnight(self):
        payload = {'2.1': {'today': {'slots': slots((1320, 1380), (1380, 1440)),
                                     'date': '2026-10-09T00:00:00+03:00',
                                     'status': 'ScheduleApplies'}}}
        (today,) = parse_group(payload, '2.1')
        self.assertEqual(today.outages,
                         (PlannedOutage(K(2026, 10, 9, 22), K(2026, 10, 10, 0)),))

    def test_autumn_dst_day_uses_wall_clock(self):
        payload = {'3.1': {'today': {'slots': slots((180, 300)),
                                     'date': '2026-10-25T00:00:00+03:00',
                                     'status': 'ScheduleApplies'}}}
        (day,) = parse_group(payload, '3.1')
        self.assertEqual(day.outages[0].start.strftime('%H:%M'), '03:00')
        self.assertEqual(day.outages[0].end.strftime('%H:%M'), '05:00')

    def test_unknown_group(self):
        self.assertEqual(parse_group(PAYLOAD, '99.1'), [])

    def test_round_trip_dict(self):
        (today, _) = parse_group(PAYLOAD, '16.1')
        self.assertEqual(day_from_dict(day_to_dict(today)), today)


class DescribeTest(unittest.TestCase):
    def test_next_outage_today(self):
        self.assertEqual(describe(days(), K(2026, 10, 9, 8)),
                         "\U0001f4c5 Наступне відключення за графіком: сьогодні 09:00–12:30")

    def test_inside_planned_outage(self):
        self.assertEqual(describe(days(), K(2026, 10, 9, 10)),
                         "\U0001f4c5 За графіком світла немає до 12:30")

    def test_next_outage_tomorrow(self):
        d = days(tomorrow=('ScheduleApplies', [(K(2026, 10, 10, 12), K(2026, 10, 10, 15, 30))]))
        self.assertEqual(describe(d, K(2026, 10, 9, 13)),
                         "\U0001f4c5 Наступне відключення за графіком: завтра 12:00–15:30")

    def test_tomorrow_not_published(self):
        d = days(tomorrow=('WaitingForSchedule', []))
        self.assertEqual(describe(d, K(2026, 10, 9, 13)),
                         "\U0001f4c5 Сьогодні відключень за графіком більше немає; "
                         "графік на завтра ще не опубліковано")

    def test_today_not_published(self):
        d = days(today=('WaitingForSchedule', []), tomorrow=('WaitingForSchedule', []))
        self.assertEqual(describe(d, K(2026, 10, 9, 13)),
                         "\U0001f4c5 Графік на сьогодні ще не опубліковано")

    def test_emergency(self):
        d = days(today=('EmergencyShutdowns', []))
        self.assertIn("аварійні", describe(d, K(2026, 10, 9, 13)))

    def test_nothing_planned(self):
        d = days(today=('NoOutages', []), tomorrow=('NoOutages', []))
        self.assertEqual(describe(d, K(2026, 10, 9, 13)),
                         "\U0001f4c5 Відключень за графіком не заплановано")

    def test_no_data(self):
        self.assertIsNone(describe([], K(2026, 10, 9, 13)))


class RemindersTest(unittest.TestCase):
    def test_due_once_inside_lead_window(self):
        sent = set()
        lead = timedelta(minutes=30)
        self.assertEqual(due_reminders(days(), K(2026, 10, 9, 8, 29), lead, sent), [])
        due = due_reminders(days(), K(2026, 10, 9, 8, 31), lead, sent)
        self.assertEqual([o.start for o in due], [K(2026, 10, 9, 9)])
        self.assertEqual(due_reminders(days(), K(2026, 10, 9, 8, 45), lead, sent), [])

    def test_not_after_start_and_not_when_schedule_suspended(self):
        lead = timedelta(minutes=30)
        self.assertEqual(due_reminders(days(), K(2026, 10, 9, 9, 1), lead, set()), [])
        d = days(today=('EmergencyShutdowns',
                        [(K(2026, 10, 9, 9), K(2026, 10, 9, 12, 30))]))
        self.assertEqual(due_reminders(d, K(2026, 10, 9, 8, 45), lead, set()), [])


class DstReminderTest(unittest.TestCase):
    """Lead time is real time, not wall-clock time, around clock changes"""
    lead = timedelta(minutes=30)

    def one(self, start):
        return [DaySchedule(start.date(), 'ScheduleApplies',
                            (PlannedOutage(start, start + timedelta(hours=2)),))]

    def test_spring_forward(self):
        # 29.03.2026 04:00 EEST = 01:00 UTC; 03:00 local does not exist
        d = self.one(K(2026, 3, 29, 4))
        now = datetime(2026, 3, 29, 0, 40, tzinfo=timezone.utc)
        self.assertEqual(len(due_reminders(d, now, self.lead, set())), 1)

    def test_fall_back(self):
        # 25.10.2026 04:00 EET = 02:00 UTC; 90 min before is too early
        d = self.one(K(2026, 10, 25, 4))
        early = datetime(2026, 10, 25, 0, 45, tzinfo=timezone.utc)
        self.assertEqual(due_reminders(d, early, self.lead, set()), [])
        on_time = datetime(2026, 10, 25, 1, 40, tzinfo=timezone.utc)
        self.assertEqual(len(due_reminders(d, on_time, self.lead, set())), 1)

    def test_same_outage_from_db_is_not_reminded_twice(self):
        # Repeated autumn hour: zoneinfo vs fixed-offset copies of one instant
        start = K(2026, 10, 25, 3, 30).replace(fold=0)
        sent = set()
        due_reminders(self.one(start), start - timedelta(minutes=10), self.lead, sent)
        restored = day_from_dict(day_to_dict(self.one(start)[0]))
        self.assertEqual(due_reminders([restored], start - timedelta(minutes=5),
                                       self.lead, sent), [])


class PlannedSecondsTest(unittest.TestCase):
    def test_clipped_to_window(self):
        d = days(today=('ScheduleApplies', [(K(2026, 10, 9, 9), K(2026, 10, 9, 12, 30)),
                                            (K(2026, 10, 9, 18), K(2026, 10, 9, 22))]))
        self.assertEqual(planned_seconds(d, K(2026, 10, 9, 10), K(2026, 10, 9, 20)),
                         (2.5 + 2) * 3600)


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = self.payload

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return payload
        return Response()


class FetchTest(unittest.TestCase):
    def test_fetch_schedule(self):
        session = FakeSession(PAYLOAD)
        days = fetch_schedule('https://x/planned-outages', '16.1', session=session)
        self.assertEqual(len(days), 2)
        self.assertEqual(session.calls[0][1]['timeout'], 10)

    def test_fetch_group(self):
        session = FakeSession({'group': 16, 'subgroup': 1})
        self.assertEqual(fetch_group('https://x/addresses/v2', 1624, 32079,
                                     session=session), '16.1')
        url, kwargs = session.calls[0]
        self.assertEqual(url, 'https://x/addresses/v2/group')
        self.assertEqual(kwargs['params'], {'regionId': 25, 'dsoId': 902,
                                            'streetId': 1624, 'houseId': 32079})


if __name__ == '__main__':
    unittest.main()
