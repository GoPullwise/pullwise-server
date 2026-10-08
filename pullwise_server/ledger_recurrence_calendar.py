"""Calendar-based expense periods; money and execution remain elsewhere."""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date")
    return parsed


def schedule_input(value):
    if not isinstance(value, dict):
        raise ValueError("schedule")
    frequency = value.get("frequency")
    selectors = {"weekly": {"weekday": 7}, "monthly": {"day": 31},
                 "quarterly": {"quarterMonth": 3, "day": 31},
                 "yearly": {"month": 12, "day": 31}}
    if not isinstance(frequency, str) or frequency not in selectors:
        raise ValueError("frequency")
    required = {"frequency", "timezone", "startOn", *selectors[frequency]}
    if not required <= set(value) or set(value) - required - {"endOn"}:
        raise ValueError("schedule fields")
    for name, maximum in selectors[frequency].items():
        if type(value[name]) is not int or not 1 <= value[name] <= maximum:
            raise ValueError(name)
    zone = value["timezone"]
    if (not isinstance(zone, str) or not 1 <= len(zone) <= 100
            or not re.fullmatch(r"[A-Za-z0-9_+./-]+", zone)):
        raise ValueError("timezone")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone") from None
    start = iso_date(value["startOn"])
    end = iso_date(value["endOn"]) if value.get("endOn") is not None else None
    if end is not None and end < start:
        raise ValueError("schedule range")
    return {"frequency": frequency, **{name: value[name] for name in selectors[frequency]},
            "timezone": zone, "startOn": start.isoformat(),
            "endOn": end.isoformat() if end else None}


def _month_day(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _month_shift(year, month, step):
    value = year * 12 + month - 1 + step
    return value // 12, value % 12 + 1


def next_occurrence(schedule, on_or_after=None):
    """Find the first original-rule date in a period; never drift after clamping."""
    start = iso_date(schedule["startOn"])
    current = max(start, on_or_after or start)
    frequency = schedule["frequency"]
    try:
        if frequency == "weekly":
            result = current + timedelta(days=(schedule["weekday"] - current.isoweekday()) % 7)
        elif frequency == "monthly":
            result = _month_day(current.year, current.month, schedule["day"])
            if result < current:
                result = _month_day(*_month_shift(current.year, current.month, 1), schedule["day"])
        elif frequency == "quarterly":
            month = ((current.month - 1) // 3) * 3 + schedule["quarterMonth"]
            result = _month_day(current.year, month, schedule["day"])
            if result < current:
                result = _month_day(*_month_shift(current.year, month, 3), schedule["day"])
        else:
            result = _month_day(current.year, schedule["month"], schedule["day"])
            if result < current:
                result = _month_day(current.year + 1, schedule["month"], schedule["day"])
    except (ValueError, OverflowError):
        return None
    end = iso_date(schedule["endOn"]) if schedule.get("endOn") else None
    return None if end is not None and result > end else result


def following_occurrence(schedule, current):
    try:
        return next_occurrence(schedule, current + timedelta(days=1))
    except OverflowError:
        return None


def period_key(schedule, occurred):
    frequency = schedule["frequency"]
    if frequency == "weekly":
        year, week, _ = occurred.isocalendar()
        return f"W{year:04d}-{week:02d}"
    if frequency == "monthly":
        return f"M{occurred.year:04d}-{occurred.month:02d}"
    if frequency == "quarterly":
        return f"Q{occurred.year:04d}-{(occurred.month - 1) // 3 + 1}"
    return f"Y{occurred.year:04d}"


def local_today(schedule, now):
    return datetime.fromtimestamp(now, ZoneInfo(schedule["timezone"])).date()


def due_timestamp(schedule, occurred):
    """Earliest local-day instant: first fold, or first valid instant after a gap."""
    zone = ZoneInfo(schedule["timezone"])
    wall = datetime.combine(occurred, time.min)
    candidates = sorted({int(wall.replace(tzinfo=zone, fold=fold).timestamp()) for fold in (0, 1)})
    valid = [stamp for stamp in candidates
             if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == wall]
    if valid:
        return min(valid)
    # ZoneInfo supplies both sides of a nonexistent wall time. Binary-search
    # the bounded transition interval, including a timezone that skipped a day.
    low, high = candidates[0], candidates[-1]
    while high - low > 1:
        middle = (low + high) // 2
        if datetime.fromtimestamp(middle, zone).replace(tzinfo=None) >= wall:
            high = middle
        else:
            low = middle
    return high


def next_fields(schedule, occurred):
    if occurred is None:
        return {"next_run_at": None, "next_occurrence_on": None, "next_period_key": None}
    stamp = due_timestamp(schedule, occurred)
    if not 0 <= stamp <= 9007199254740991:
        raise ValueError("schedule date is outside the supported execution range")
    return {"next_run_at": stamp, "next_occurrence_on": occurred.isoformat(),
            "next_period_key": period_key(schedule, occurred)}
