from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class PowerSnapshot:
    site_id: int
    site_name: str
    site_identifier: str
    battery_soc_percent: float | None
    solar_watts: float | None
    house_watts: float | None
    generator_watts: float | None
    active_input_source: int | None
    queried_at_unix_ms: int | None
    queried_at_iso: str | None

    def to_dict(self) -> dict[str, float | int | str | None]:
        return asdict(self)

    @classmethod
    def with_timestamp(
        cls,
        *,
        site_id: int,
        site_name: str,
        site_identifier: str,
        battery_soc_percent: float | None,
        solar_watts: float | None,
        house_watts: float | None,
        generator_watts: float | None,
        active_input_source: int | None,
        queried_at_unix_ms: int | None,
    ) -> "PowerSnapshot":
        queried_at_iso = None
        if queried_at_unix_ms is not None:
            queried_at_iso = datetime.fromtimestamp(
                queried_at_unix_ms / 1000,
                tz=UTC,
            ).isoformat()

        return cls(
            site_id=site_id,
            site_name=site_name,
            site_identifier=site_identifier,
            battery_soc_percent=battery_soc_percent,
            solar_watts=solar_watts,
            house_watts=house_watts,
            generator_watts=generator_watts,
            active_input_source=active_input_source,
            queried_at_unix_ms=queried_at_unix_ms,
            queried_at_iso=queried_at_iso,
        )
