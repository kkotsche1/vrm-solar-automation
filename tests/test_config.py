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
                        "SUNSHINE_HOURS_MIN=4.5",
                        "BATTERY_MIN_SOC_PERCENT=45",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            settings = load_settings(env_path)

        self.assertEqual(settings.sunshine_hours_min, 4.5)
        self.assertEqual(settings.battery_min_soc_percent, 45.0)

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
