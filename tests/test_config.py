from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vrm_solar_automation.config import load_settings


class ConfigTests(unittest.TestCase):
    def test_load_settings_rejects_invalid_local_time_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("AUTO_OFF_START_LOCAL=8pm\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_rejects_invalid_auto_control_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("AUTO_CONTROL_TIMEZONE=Not/A_Real_Zone\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_uses_weather_timezone_as_auto_control_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "WEATHER_TIMEZONE=Europe/Amsterdam",
                        "AUTO_OFF_START_LOCAL=18:00",
                        "AUTO_RESUME_START_LOCAL=08:00",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            settings = load_settings(env_path)

        self.assertEqual(settings.auto_control_timezone, "Europe/Amsterdam")
        self.assertEqual(settings.auto_off_start_local, "18:00")
        self.assertEqual(settings.auto_resume_start_local, "08:00")

    def test_load_settings_parses_soc_and_sunshine_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "CERBO_FETCH_RETRY_COUNT=4",
                        "CERBO_FETCH_RETRY_DELAY_SECONDS=1.5",
                        "CERBO_UNAVAILABLE_GRACE_CYCLES=5",
                        "SUNSHINE_HOURS_MIN=4.5",
                        "BATTERY_MIN_SOC_PERCENT=45",
                        "BATTERY_SOFT_MIN_SOC_PERCENT=35",
                        "BATTERY_HARD_MIN_SOC_PERCENT=30",
                        "BATTERY_CAPACITY_KWH=50",
                        "FORECAST_LIBERAL_SUNSHINE_HOURS_MIN=9.0",
                        "FORECAST_LIBERAL_SUNSHINE_HOURS_MAX=12.0",
                        "DAY_MORNING_BIAS_END_LOCAL=11:00",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            settings = load_settings(env_path)

        self.assertEqual(settings.cerbo_fetch_retry_count, 4)
        self.assertEqual(settings.cerbo_fetch_retry_delay_seconds, 1.5)
        self.assertEqual(settings.cerbo_unavailable_grace_cycles, 5)
        self.assertEqual(settings.sunshine_hours_min, 4.5)
        self.assertEqual(settings.battery_min_soc_percent, 45.0)
        self.assertEqual(settings.battery_soft_min_soc_percent, 35.0)
        self.assertEqual(settings.battery_hard_min_soc_percent, 30.0)
        self.assertEqual(settings.battery_capacity_kwh, 50.0)
        self.assertEqual(settings.forecast_liberal_sunshine_hours_min, 9.0)
        self.assertEqual(settings.forecast_liberal_sunshine_hours_max, 12.0)
        self.assertEqual(settings.day_morning_bias_end_local, "11:00")

    def test_load_settings_parses_surplus_night_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "SURPLUS_NIGHT_ENABLED=false",
                        "SURPLUS_NIGHT_BASE_LOAD_KW=1.7",
                        "SURPLUS_NIGHT_HARD_MIN_SOC_PERCENT=25",
                        "SURPLUS_NIGHT_BUFFER_SOC_PERCENT=6",
                        "SURPLUS_NIGHT_TURN_ON_MARGIN_SOC_PERCENT=12",
                        "SURPLUS_NIGHT_TURN_OFF_MARGIN_SOC_PERCENT=7",
                        "SURPLUS_NIGHT_MIN_TURN_ON_MARGIN_SOC_PERCENT=8",
                        "SURPLUS_NIGHT_MIN_TURN_OFF_MARGIN_SOC_PERCENT=3",
                        "SURPLUS_NIGHT_NEXT_DAY_SUNSHINE_MIN=9.5",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            settings = load_settings(env_path)

        self.assertFalse(settings.surplus_night_enabled)
        self.assertEqual(settings.surplus_night_base_load_kw, 1.7)
        self.assertEqual(settings.surplus_night_hard_min_soc_percent, 25.0)
        self.assertEqual(settings.surplus_night_buffer_soc_percent, 6.0)
        self.assertEqual(settings.surplus_night_turn_on_margin_soc_percent, 12.0)
        self.assertEqual(settings.surplus_night_turn_off_margin_soc_percent, 7.0)
        self.assertEqual(settings.surplus_night_min_turn_on_margin_soc_percent, 8.0)
        self.assertEqual(settings.surplus_night_min_turn_off_margin_soc_percent, 3.0)
        self.assertEqual(settings.surplus_night_next_day_sunshine_min, 9.5)

    def test_load_settings_rejects_invalid_sunshine_hours_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("SUNSHINE_HOURS_MIN=25\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_rejects_invalid_soc_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("BATTERY_MIN_SOC_PERCENT=110\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_rejects_invalid_cerbo_retry_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("CERBO_FETCH_RETRY_COUNT=abc\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_rejects_invalid_soc_threshold_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "BATTERY_MIN_SOC_PERCENT=45",
                        "BATTERY_SOFT_MIN_SOC_PERCENT=35",
                        "BATTERY_HARD_MIN_SOC_PERCENT=36",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_rejects_invalid_liberal_forecast_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "FORECAST_LIBERAL_SUNSHINE_HOURS_MIN=12",
                        "FORECAST_LIBERAL_SUNSHINE_HOURS_MAX=9",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_rejects_removed_hysteresis_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("BATTERY_OFF_BELOW_SOC_PERCENT=40\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_rejects_removed_seasonal_quiet_hours_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "SUMMER_START_MONTH_DAY=04-01",
                        "WINTER_START_MONTH_DAY=10-01",
                        "SUMMER_AUTO_OFF_START_LOCAL=18:30",
                        "SUMMER_AUTO_RESUME_START_LOCAL=08:30",
                        "WINTER_AUTO_OFF_START_LOCAL=17:30",
                        "WINTER_AUTO_RESUME_START_LOCAL=09:00",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_settings(env_path)

    def test_load_settings_parses_gmail_smtp_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "SMTP_GMAIL_SENDER=alerts@example.com",
                        "SMTP_GMAIL_APP_PASSWORD=app-password",
                        "SMTP_GMAIL_RECIPIENTS= one@example.com, two@example.com , ,three@example.com ",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            settings = load_settings(env_path)

        self.assertEqual(settings.smtp_gmail_sender, "alerts@example.com")
        self.assertEqual(settings.smtp_gmail_app_password, "app-password")
        self.assertEqual(
            settings.smtp_gmail_recipients,
            ("one@example.com", "two@example.com", "three@example.com"),
        )

    def test_load_settings_parses_database_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DATABASE_URL=sqlite:////tmp/automation.db",
                        "DATABASE_AUTO_MIGRATE=true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            settings = load_settings(env_path)

        self.assertEqual(settings.database_url, "sqlite:////tmp/automation.db")
        self.assertTrue(settings.database_auto_migrate)


if __name__ == "__main__":
    unittest.main()
