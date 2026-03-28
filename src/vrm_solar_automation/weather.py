from __future__ import annotations

from dataclasses import dataclass

import aiohttp

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class WeatherSnapshot:
    current_temperature_c: float | None
    today_min_temperature_c: float | None
    today_max_temperature_c: float | None
    weather_code: int | None
    queried_timezone: str

    def to_dict(self) -> dict[str, float | int | str | None]:
        return {
            "current_temperature_c": self.current_temperature_c,
            "today_min_temperature_c": self.today_min_temperature_c,
            "today_max_temperature_c": self.today_max_temperature_c,
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
            "daily": "temperature_2m_min,temperature_2m_max,weather_code",
            "forecast_days": 1,
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
            weather_code=_first_int_item(daily.get("weather_code")),
            queried_timezone=str(data.get("timezone", timezone)),
        )


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
