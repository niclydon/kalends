"""Reference adapters for the source systems every fleet has."""

from .base import AdapterResult, SourceAdapter
from .cron_adapter import CronAdapter
from .postgres_actuals_adapter import CompletionPointsAdapter, FireLogAdapter
from .postgres_bands_adapter import ServiceStatusAdapter, WorkClaimsAdapter
from .postgres_jobs_adapter import PostgresJobsAdapter
from .systemd_adapter import SystemdAdapter
from .yaml_calendar_adapter import YamlCalendarAdapter

__all__ = [
    "AdapterResult",
    "CompletionPointsAdapter",
    "CronAdapter",
    "FireLogAdapter",
    "PostgresJobsAdapter",
    "ServiceStatusAdapter",
    "SourceAdapter",
    "SystemdAdapter",
    "WorkClaimsAdapter",
    "YamlCalendarAdapter",
]
