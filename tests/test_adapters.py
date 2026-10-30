from datetime import datetime, timezone

from kalends.adapters import (
    CronAdapter,
    PostgresJobsAdapter,
    SystemdAdapter,
    YamlCalendarAdapter,
)
from kalends.projector import project

NOW = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


# --- systemd -------------------------------------------------------------------

def test_systemd_fixture(examples_dir):
    result = SystemdAdapter.from_files(
        examples_dir / "systemd-timers.json",
        examples_dir / "systemd-units.json",
        source_id="systemd",
        plane="host-a",
    ).collect()
    by_unit = {s.source_native_id: s for s in result.series}
    assert len(result.series) == 8

    backup = by_unit["backup-volumes.timer"]
    assert backup.cadence_dialect == "systemd-calendar"
    assert backup.cadence_expr == "*-*-* 03:00:00"
    assert backup.rrule == "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0"
    assert backup.plane == "host-a"

    sync = by_unit["model-sync.timer"]
    assert sync.plane == "host-b"
    assert sync.rrule is not None and "BYDAY=MO,TU,WE,TH,FR" in sync.rrule

    watchdog = by_unit["watchdog-feed.timer"]
    assert watchdog.cadence_dialect == "systemd-relative"
    assert watchdog.rrule is None
    assert "OnUnitActiveSec" in (watchdog.cadence_expr or "")

    # Every timer except the boot-only one has a NEXT hint and a LAST actual.
    assert len(result.instances) == 7
    assert len(result.actuals) == 7
    assert all(i.series_id != by_unit["boot-provision.timer"].series_id
               for i in result.instances)


# --- cron ------------------------------------------------------------------------

def test_cron_fixture(examples_dir):
    result = CronAdapter.from_file(
        examples_dir / "crontab", source_id="crontab", plane="host-a",
        owner_id="ops",
    ).collect()
    # 6 job lines; the comment, blank line, and MAILTO assignment are not jobs.
    assert len(result.series) == 6
    by_title = {s.title: s for s in result.series}
    probe = by_title["/opt/fleet/heartbeat-probe"]
    assert probe.cadence_expr == "*/20 * * * *"
    assert probe.rrule == "FREQ=MINUTELY;INTERVAL=20"
    weekly = by_title["/usr/local/bin/week-open-report"]
    assert weekly.rrule is not None and "BYDAY=MO" in weekly.rrule
    reboot = by_title["/usr/local/bin/boot-note"]
    assert reboot.cadence_expr == "@reboot"
    assert reboot.rrule is None  # honest: boot-time work has no RRULE


# --- YAML calendar -----------------------------------------------------------------

def test_yaml_fixture(examples_dir):
    result = YamlCalendarAdapter.from_file(
        examples_dir / "patrols.yaml", source_id="patrol-calendar",
        plane="platform",
    ).collect()
    by_name = {s.source_native_id: s for s in result.series}
    assert len(result.series) == 6

    audit = by_name["harbour-light-audit"]
    assert audit.rrule == "FREQ=WEEKLY"
    assert audit.resource_claims == {"slot_group": "survey"}
    assert audit.owner_kind == "agent" and audit.owner_id == "crimson"

    digest = by_name["beacon-health-digest"]
    assert digest.owner_kind == "human-facing"

    parked = by_name["deep-archive-sweep"]
    assert parked.lifecycle_state == "dormant"
    assert parked.rrule == "FREQ=MONTHLY;INTERVAL=3"

    storm = by_name["storm-watch"]
    assert storm.cadence_dialect == "none"
    assert storm.rrule is None
    assert storm.event_triggers == ["weather-alert"]


# --- Postgres registry + queue ------------------------------------------------------

def _postgres_result(examples_dir):
    return PostgresJobsAdapter.from_files(
        examples_dir / "jobs-config.yaml",
        examples_dir / "jobs.json",
        examples_dir / "queue.json",
    ).collect()


def test_postgres_series_mapping(examples_dir):
    result = _postgres_result(examples_dir)
    by_name = {s.source_native_id: s for s in result.series}
    # recurring (3) + self-chain (2) become series; "none" and "event" do not.
    assert set(by_name) == {
        "nightly-rollup",
        "tide-gauge-poll",
        "drift-correction",
        "beacon-relay",
        "legacy-purge",
    }
    assert by_name["nightly-rollup"].rrule == "FREQ=DAILY;INTERVAL=1"
    assert by_name["nightly-rollup"].plane == "host-a"
    assert by_name["tide-gauge-poll"].plane == "platform"  # null host: logical plane
    assert by_name["drift-correction"].cadence_dialect == "self-chain"
    assert by_name["drift-correction"].rrule is None
    assert by_name["legacy-purge"].lifecycle_state == "retired"
    assert result.stats["kinds"] == {
        "recurring": 3,
        "self-chain": 2,
        "none": 1,
        "event": 1,
    }


def test_postgres_queue_instances_and_band(examples_dir):
    result = _postgres_result(examples_dir)
    assert len(result.instances) == 3  # the three pending rows
    starts = {i.title: i.planned_start for i in result.instances}
    assert starts["drift-correction"] == datetime(
        2026, 10, 30, 6, 15, tzinfo=timezone.utc
    )
    assert len(result.bands) == 1
    band = result.bands[0]
    assert band.title == "tide-gauge-poll"
    assert band.owner_id == "worker-7"
    assert band.resource_claims == {"worker": "worker-7"}
    assert band.lease_expires_at == datetime(
        2026, 10, 30, 5, 21, tzinfo=timezone.utc
    )
    assert band.last_heartbeat_at == datetime(
        2026, 10, 30, 5, 11, tzinfo=timezone.utc
    )


def test_postgres_plus_projector_self_chain(examples_dir):
    result = _postgres_result(examples_dir)
    out = project(
        result.series,
        provided_instances=result.instances,
        bands=result.bands,
        sources=[result.source],
        now=NOW,
    )
    chain = [i for i in out.instances if i.title == "drift-correction"]
    assert len(chain) == 1
    assert chain[0].planned_start == datetime(
        2026, 10, 30, 6, 15, tzinfo=timezone.utc
    )
    assert [i for i in out.instances if i.title == "legacy-purge"] == []
    assert len(out.bands) == 1
