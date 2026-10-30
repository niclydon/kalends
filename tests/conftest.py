from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "example-fleet"

# The example fleet's fixtures are dated around this moment; freezing
# "now" here keeps every horizon assertion stable.
FROZEN_NOW = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLES


@pytest.fixture
def frozen_now() -> datetime:
    return FROZEN_NOW
