"""Kalends: one calendar for every agent, job, and long-running task
in a multi-agent fleet. A projection over existing schedulers, not a
new scheduler.
"""

from .model import Actual, Band, Instance, Series, Source, Window
from .projector import Projection, project

__version__ = "0.1.0"

__all__ = [
    "Actual",
    "Band",
    "Instance",
    "Projection",
    "Series",
    "Source",
    "Window",
    "project",
    "__version__",
]
