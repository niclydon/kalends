"""Reference adapters for the source systems every fleet has."""

from .base import AdapterResult, SourceAdapter
from .cron_adapter import CronAdapter
from .postgres_jobs_adapter import PostgresJobsAdapter
from .systemd_adapter import SystemdAdapter
from .yaml_calendar_adapter import YamlCalendarAdapter

__all__ = [
    "AdapterResult",
    "CronAdapter",
    "PostgresJobsAdapter",
    "SourceAdapter",
    "SystemdAdapter",
    "YamlCalendarAdapter",
]
