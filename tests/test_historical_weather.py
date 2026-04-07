from __future__ import annotations

from datetime import date
import unittest

from vrm_solar_automation.historical_weather import (
    _augment_rows_with_weather,
    _chunk_date_ranges,
    _parse_daily_weather_payload,
    DailyWeather,
)


class HistoricalWeatherTests(unittest.TestCase):
    def test_parse_daily_weather_payload_converts_sunshine_seconds_to_hours(self) -> None:
        payload = {
            "daily": {
                "time": ["2026-03-16", "2026-03-17"],
                "temperature_2m_min": [11.2, 10.0],
                "temperature_2m_max": [21.6, 20.2],
                "temperature_2m_mean": [16.1, 15.0],
                "sunshine_duration": [18000, None],
                "weather_code": [3, 61],
            }
        }

        result = _parse_daily_weather_payload(payload)

        self.assertEqual(result[date(2026, 3, 16)].sunshine_hours, 5.0)
        self.assertIsNone(result[date(2026, 3, 17)].sunshine_hours)
        self.assertEqual(result[date(2026, 3, 17)].weather_code, 61)

    def test_augment_rows_with_weather_preserves_three_header_rows(self) -> None:
        header_rows = [
            ["timestamp", "Solar"],
            ["Europe/Paris (+02:00)", "Solar watts"],
            ["", "W"],
        ]
        data_rows = [
            ["2026-03-16 14:17:53", "1200"],
            ["2026-03-16 14:18:53", "1300"],
            ["2026-03-17 14:18:53", "1100"],
        ]
        weather_by_date = {
            date(2026, 3, 16): DailyWeather(
                local_date=date(2026, 3, 16),
                min_temperature_c=10.0,
                max_temperature_c=21.0,
                mean_temperature_c=15.5,
                sunshine_hours=5.5,
                weather_code=3,
            )
        }

        rows, enriched_rows, missing_rows = _augment_rows_with_weather(
            header_rows=header_rows,
            data_rows=data_rows,
            timestamp_column=0,
            weather_by_date=weather_by_date,
        )

        self.assertEqual(rows[0][-1], "Weather [Open-Meteo Archive]")
        self.assertEqual(rows[1][-1], "Weather code")
        self.assertEqual(rows[2][-2], "h")
        self.assertEqual(rows[3][-6], "2026-03-16")
        self.assertEqual(rows[3][-1], "3")
        self.assertEqual(rows[5][-6:], ["", "", "", "", "", ""])
        self.assertEqual(enriched_rows, 2)
        self.assertEqual(missing_rows, 1)

    def test_chunk_date_ranges_groups_values(self) -> None:
        days = [
            date(2026, 3, 16),
            date(2026, 3, 17),
            date(2026, 3, 18),
            date(2026, 3, 25),
        ]

        ranges = _chunk_date_ranges(days, chunk_days=3)

        self.assertEqual(
            ranges,
            [
                (date(2026, 3, 16), date(2026, 3, 18)),
                (date(2026, 3, 25), date(2026, 3, 25)),
            ],
        )


if __name__ == "__main__":
    unittest.main()
