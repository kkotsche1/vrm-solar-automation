from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import aiohttp

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class WeatherSnapshot:
    current_temperature_c: float | None
    today_min_temperature_c: float | None
    today_max_temperature_c: float | None
    today_sunshine_hours: float | None
    weather_code: int | None
    queried_timezone: str
    tomorrow_sunshine_hours: float | None = None
    # Local wall-clock ISO timestamps, e.g. "2026-09-04T07:19". Open-Meteo returns these in
    # the requested timezone, so they are naive by construction and compared as wall clocks.
    today_sunrise_iso: str | None = None
    tomorrow_sunrise_iso: str | None = None
    today_sunset_iso: str | None = None
    tomorrow_sunset_iso: str | None = None

    def sunrise_minutes_for(self, *, tomorrow: bool) -> int | None:
        """Minutes past local midnight of the relevant sunrise, or None if unavailable."""
        return _minutes_past_midnight(
            self.tomorrow_sunrise_iso if tomorrow else self.today_sunrise_iso
        )

    def sunset_minutes_for(self, *, tomorrow: bool) -> int | None:
        return _minutes_past_midnight(
            self.tomorrow_sunset_iso if tomorrow else self.today_sunset_iso
        )

    def to_dict(self) -> dict[str, float | int | str | None]:
        return {
            "current_temperature_c": self.current_temperature_c,
            "today_min_temperature_c": self.today_min_temperature_c,
            "today_max_temperature_c": self.today_max_temperature_c,
            "today_sunshine_hours": self.today_sunshine_hours,
            "tomorrow_sunshine_hours": self.tomorrow_sunshine_hours,
            "today_sunrise_iso": self.today_sunrise_iso,
            "tomorrow_sunrise_iso": self.tomorrow_sunrise_iso,
            "today_sunset_iso": self.today_sunset_iso,
            "tomorrow_sunset_iso": self.tomorrow_sunset_iso,
            "weather_code": self.weather_code,
            "queried_timezone": self.queried_timezone,
        }


class OpenMeteoClient:
    async def fetch_weather(
        self,
        *,
        session: aiohttp.ClientSession,
        latitude: float,
        longitude: float,
        timezone: str,
    ) -> WeatherSnapshot:
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
            "current": "temperature_2m",
            "daily": (
                "temperature_2m_min,temperature_2m_max,weather_code,sunshine_duration,"
                "sunrise,sunset"
            ),
            "forecast_days": 2,
        }
        async with session.get(FORECAST_URL, params=params) as response:
            response.raise_for_status()
            data = await response.json()

        current = data.get("current", {})
        daily = data.get("daily", {})

        return WeatherSnapshot(
            current_temperature_c=_first_item(current.get("temperature_2m")),
            today_min_temperature_c=_first_item(daily.get("temperature_2m_min")),
            today_max_temperature_c=_first_item(daily.get("temperature_2m_max")),
            today_sunshine_hours=_first_duration_hours(daily.get("sunshine_duration")),
            weather_code=_first_int_item(daily.get("weather_code")),
            queried_timezone=str(data.get("timezone", timezone)),
            tomorrow_sunshine_hours=_duration_hours_at(daily.get("sunshine_duration"), index=1),
            today_sunrise_iso=_str_at(daily.get("sunrise"), index=0),
            tomorrow_sunrise_iso=_str_at(daily.get("sunrise"), index=1),
            today_sunset_iso=_str_at(daily.get("sunset"), index=0),
            tomorrow_sunset_iso=_str_at(daily.get("sunset"), index=1),
        )


def _str_at(value: list[str] | str | None, *, index: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        if index < 0 or index >= len(value):
            return None
        item = value[index]
        return str(item) if item is not None else None
    return str(value) if index == 0 else None


def _minutes_past_midnight(value: str | None) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.hour * 60 + moment.minute


def _first_item(value: list[float] | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        return float(value[0])
    return float(value)


def _first_int_item(value: list[int] | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        return int(value[0])
    return int(value)


def _first_duration_hours(value: list[float] | float | None) -> float | None:
    seconds = _first_item(value)
    if seconds is None:
        return None
    return seconds / 3600.0


def _duration_hours_at(value: list[float] | float | None, *, index: int) -> float | None:
    seconds = _item_at(value, index=index)
    if seconds is None:
        return None
    return seconds / 3600.0


def _item_at(value: list[float] | float | None, *, index: int) -> float | None:
    if value is None:
        return None
    if isinstance(value, list):
        if index < 0 or index >= len(value):
            return None
        return float(value[index])
    if index != 0:
        return None
    return float(value)
