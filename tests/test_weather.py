from __future__ import annotations

import unittest

from vrm_solar_automation.weather import OpenMeteoClient


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, object]:
        return self._payload


class FakeSession:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, params: dict[str, object]) -> FakeResponse:
        self.calls.append((url, params))
        return FakeResponse(self._payload)


class OpenMeteoWeatherTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_weather_returns_today_and_tomorrow_sunshine(self) -> None:
        payload = {
            "timezone": "Europe/Madrid",
            "current": {"temperature_2m": 12.5},
            "daily": {
                "temperature_2m_min": [8.0, 9.0],
                "temperature_2m_max": [18.0, 19.0],
                "weather_code": [3, 2],
                "sunshine_duration": [21_600, 32_400],
            },
        }
        session = FakeSession(payload)

        snapshot = await OpenMeteoClient().fetch_weather(
            session=session,
            latitude=39.7,
            longitude=2.7,
            timezone="Europe/Madrid",
        )

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["forecast_days"], 2)
        self.assertEqual(snapshot.today_sunshine_hours, 6.0)
        self.assertEqual(snapshot.tomorrow_sunshine_hours, 9.0)
        self.assertEqual(snapshot.queried_timezone, "Europe/Madrid")


if __name__ == "__main__":
    unittest.main()
