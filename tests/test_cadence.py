from kalends.cadence import (
    classify_systemd_timer,
    cron_macro_to_rrule,
    cron_to_rrule,
    interval_to_rrule,
    normalize,
    systemd_calendar_to_rrule,
    yaml_cadence_to_rrule,
)


# --- cron -------------------------------------------------------------------

def test_cron_daily_clock():
    assert cron_to_rrule("15 2 * * *") == "FREQ=DAILY;BYHOUR=2;BYMINUTE=15;BYSECOND=0"


def test_cron_weekly_weekday():
    assert (
        cron_to_rrule("0 6 * * Mon")
        == "FREQ=WEEKLY;BYDAY=MO;BYHOUR=6;BYMINUTE=0;BYSECOND=0"
    )


def test_cron_minute_step():
    assert cron_to_rrule("*/20 * * * *") == "FREQ=MINUTELY;INTERVAL=20"


def test_cron_hour_step():
    assert cron_to_rrule("0 */2 * * *") == "FREQ=HOURLY;INTERVAL=2;BYMINUTE=0;BYSECOND=0"


def test_cron_monthly_day():
    assert (
        cron_to_rrule("30 4 1 * *")
        == "FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=4;BYMINUTE=30;BYSECOND=0"
    )


def test_cron_yearly():
    assert (
        cron_to_rrule("0 0 1 1 *")
        == "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0;BYSECOND=0"
    )


def test_cron_dom_and_dow_both_restricted_is_honest_none():
    # Cron fires when either matches; RRULE would require both.
    assert cron_to_rrule("0 9 15 * Mon") is None


def test_cron_garbage_is_none():
    assert cron_to_rrule("0 0 * * FUNDAY") is None
    assert cron_to_rrule("not a cron line") is None


def test_cron_macros():
    assert cron_macro_to_rrule("@daily") == "FREQ=DAILY;BYHOUR=0;BYMINUTE=0;BYSECOND=0"
    assert cron_macro_to_rrule("@weekly") == (
        "FREQ=WEEKLY;BYDAY=SU;BYHOUR=0;BYMINUTE=0;BYSECOND=0"
    )
    assert cron_macro_to_rrule("@reboot") is None


# --- systemd calendar ---------------------------------------------------------

def test_systemd_daily():
    assert (
        systemd_calendar_to_rrule("*-*-* 03:00:00")
        == "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0"
    )


def test_systemd_weekday_single():
    assert (
        systemd_calendar_to_rrule("Sun *-*-* 04:30:00")
        == "FREQ=WEEKLY;BYDAY=SU;BYHOUR=4;BYMINUTE=30;BYSECOND=0"
    )


def test_systemd_weekday_range():
    assert (
        systemd_calendar_to_rrule("Mon..Fri *-*-* 06:00:00")
        == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=6;BYMINUTE=0;BYSECOND=0"
    )


def test_systemd_monthly_day():
    assert (
        systemd_calendar_to_rrule("*-*-01 02:00:00")
        == "FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=2;BYMINUTE=0;BYSECOND=0"
    )


def test_systemd_wildcard_hour():
    assert (
        systemd_calendar_to_rrule("*-*-* *:07:00")
        == "FREQ=HOURLY;BYMINUTE=7;BYSECOND=0"
    )


def test_systemd_keyword():
    assert (
        systemd_calendar_to_rrule("daily")
        == "FREQ=DAILY;BYHOUR=0;BYMINUTE=0;BYSECOND=0"
    )


def test_systemd_relative_is_honest_none():
    assert systemd_calendar_to_rrule("OnBootUSec=10min; every 10min (OnUnitActive)") is None
    assert normalize("systemd-relative", "every 10min (OnUnitActive)").rrule is None
    assert classify_systemd_timer(None, "OnUnitActiveSec=20min") == "systemd-relative"
    assert classify_systemd_timer("*-*-* 03:00:00") == "systemd-calendar"


def test_systemd_pinned_year_is_none():
    assert systemd_calendar_to_rrule("2027-01-01 00:00:00") is None


# --- interval strings ---------------------------------------------------------

def test_interval_every():
    assert interval_to_rrule("every 30m") == "FREQ=MINUTELY;INTERVAL=30"
    assert interval_to_rrule("every 1h") == "FREQ=HOURLY;INTERVAL=1"
    assert interval_to_rrule("every 6h") == "FREQ=HOURLY;INTERVAL=6"
    assert interval_to_rrule("every 45s") == "FREQ=SECONDLY;INTERVAL=45"
    assert interval_to_rrule("every hour") == "FREQ=HOURLY;INTERVAL=1"


def test_interval_clock_forms():
    assert interval_to_rrule("daily at 09:07:00") == (
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=7;BYSECOND=0"
    )
    assert interval_to_rrule("weekly at 09:07") == (
        "FREQ=WEEKLY;BYHOUR=9;BYMINUTE=7;BYSECOND=0"
    )
    assert interval_to_rrule("weekly on Tue at 09:30") == (
        "FREQ=WEEKLY;BYDAY=TU;BYHOUR=9;BYMINUTE=30;BYSECOND=0"
    )


# --- YAML words ----------------------------------------------------------------

def test_yaml_words():
    assert yaml_cadence_to_rrule("weekly") == "FREQ=WEEKLY"
    assert yaml_cadence_to_rrule("monthly") == "FREQ=MONTHLY"
    assert yaml_cadence_to_rrule("quarterly") == "FREQ=MONTHLY;INTERVAL=3"
    assert yaml_cadence_to_rrule("none") is None


# --- self-chain and the dispatcher ----------------------------------------------

def test_self_chain_is_honest_none():
    result = normalize("self-chain", "idle_delay_seconds=900")
    assert result.rrule is None
    assert result.dialect == "self-chain"
    assert result.expr == "idle_delay_seconds=900"


def test_normalize_preserves_original_expression():
    result = normalize("cron", "15 2 * * *")
    assert result.expr == "15 2 * * *"
    assert result.rrule == "FREQ=DAILY;BYHOUR=2;BYMINUTE=15;BYSECOND=0"
    none_result = normalize("none", None)
    assert none_result.rrule is None and none_result.expr is None
