from datetime import datetime, timezone

from kalends.expand import expand_rrule

START = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)


def test_daily_count_over_fixed_horizon():
    found = expand_rrule(
        "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0", START, START, END
    )
    # 03:00 on Oct 23 .. Nov 12 inclusive; Nov 13 03:00 is past the end.
    assert len(found) == 21
    assert found[0] == datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc)
    assert found[-1] == datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)


def test_weekly_count_over_fixed_horizon():
    found = expand_rrule("FREQ=WEEKLY;BYDAY=MO", START, START, END)
    assert [d.day for d in found] == [26, 2, 9]  # Mondays Oct 26, Nov 2, Nov 9
    assert all(d.weekday() == 0 for d in found)


def test_interval_phase_follows_dtstart():
    found = expand_rrule(
        "FREQ=MINUTELY;INTERVAL=30",
        datetime(2026, 10, 23, 0, 10, tzinfo=timezone.utc),
        START,
        datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc),
    )
    assert [d.minute for d in found] == [10, 40, 10, 40]


def test_broken_rule_expands_to_nothing():
    assert expand_rrule("FREQ=SIDEREAL", START, START, END) == []
