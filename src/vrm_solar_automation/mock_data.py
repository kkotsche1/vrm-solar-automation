from __future__ import annotations

from datetime import UTC, datetime

from .config import Settings
from .models import PowerSnapshot


def build_mock_power_snapshot(settings: Settings) -> PowerSnapshot:
    return PowerSnapshot.with_timestamp(
        site_id=settings.site_id or 0,
        site_name=settings.cerbo_site_name,
        site_identifier=settings.cerbo_site_identifier,
        battery_soc_percent=78.0,
        solar_watts=2850.0,
        house_watts=940.0,
        generator_watts=0.0,
        active_input_source=240,
        queried_at_unix_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
