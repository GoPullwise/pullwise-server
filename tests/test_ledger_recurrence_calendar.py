from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from pullwise_server.ledger_recurrence_calendar import (
    due_timestamp, following_occurrence, local_today, next_fields, next_occurrence,
    period_key, schedule_input,
)


def schedule(**changes):
    return schedule_input({"frequency": "monthly", "day": 31, "timezone": "Asia/Shanghai",
                           "startOn": "2026-01-01", **changes})


def test_month_end_clamping_retains_original_day_and_full_year_rollover():
    rule = schedule()
    current = next_occurrence(rule)
    dates = []
    for _ in range(14):
        dates.append(current.isoformat())
        current = following_occurrence(rule, current)
    assert dates[:4] == ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]
    assert dates[-3:] == ["2026-12-31", "2027-01-31", "2027-02-28"]
    assert rule["day"] == 31


def test_yearly_leap_day_recovers_on_next_leap_year():
    rule = schedule_input({"frequency": "yearly", "month": 2, "day": 29,
                           "timezone": "UTC", "startOn": "2023-01-01"})
    current = next_occurrence(rule)
    dates = []
    for _ in range(6):
        dates.append(current.isoformat())
        current = following_occurrence(rule, current)
    assert dates == ["2023-02-28", "2024-02-29", "2025-02-28", "2026-02-28", "2027-02-28", "2028-02-29"]


@pytest.mark.parametrize("quarter_month,expected", [
    (1, ["2026-01-31", "2026-04-30", "2026-07-31", "2026-10-31", "2027-01-31"]),
    (2, ["2026-02-28", "2026-05-31", "2026-08-31", "2026-11-30", "2027-02-28"]),
    (3, ["2026-03-31", "2026-06-30", "2026-09-30", "2026-12-31", "2027-03-31"]),
])
def test_natural_quarter_months_are_not_rolling_three_month_offsets(quarter_month, expected):
    rule = schedule(frequency="quarterly", quarterMonth=quarter_month)
    current, dates = next_occurrence(rule), []
    for _ in range(5):
        dates.append(current.isoformat())
        current = following_occurrence(rule, current)
    assert dates == expected


def test_weekday_and_iso_week_year_cross_calendar_year():
    rule = schedule_input({"frequency": "weekly", "weekday": 7, "timezone": "UTC", "startOn": "2020-12-31"})
    first = next_occurrence(rule)
    assert first == date(2021, 1, 3)
    assert period_key(rule, first) == "W2020-53"
    assert period_key(rule, following_occurrence(rule, first)) == "W2021-01"


@pytest.mark.parametrize("zone,business_date,expected_utc,expected_wall", [
    ("America/Santiago", "2019-09-08", "2019-09-08T04:00:00+00:00", "2019-09-08T01:00:00"),
    ("America/Havana", "2020-11-01", "2020-11-01T04:00:00+00:00", "2020-11-01T00:00:00"),
    ("Pacific/Apia", "2011-12-30", "2011-12-30T10:00:00+00:00", "2011-12-31T00:00:00"),
])
def test_iana_midnight_gap_fold_and_skipped_day_choose_first_valid_instant(zone, business_date, expected_utc, expected_wall):
    rule = schedule(timezone=zone)
    stamp = due_timestamp(rule, date.fromisoformat(business_date))
    assert datetime.fromtimestamp(stamp, timezone.utc).isoformat() == expected_utc
    assert datetime.fromtimestamp(stamp, ZoneInfo(zone)).replace(tzinfo=None).isoformat() == expected_wall


def test_local_day_and_start_end_range_are_explicit():
    rule = schedule(timezone="America/New_York", endOn="2026-01-31")
    now = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    assert local_today(rule, now) == date(2025, 12, 31)
    first = next_occurrence(rule)
    assert first == date(2026, 1, 31)
    assert following_occurrence(rule, first) is None
    assert next_fields(rule, None) == {"next_run_at": None, "next_occurrence_on": None, "next_period_key": None}


@pytest.mark.parametrize("changes", [
    {"day": True}, {"day": 0}, {"day": 32}, {"timezone": "../UTC"},
    {"timezone": "Not/A_Zone"}, {"startOn": "2026-02-30"}, {"endOn": "2025-12-31"},
    {"unknown": 1}, {"frequency": "daily"}, {"frequency": "weekly", "weekday": 1},
])
def test_invalid_calendar_rules_are_rejected(changes):
    with pytest.raises(ValueError):
        schedule(**changes)
