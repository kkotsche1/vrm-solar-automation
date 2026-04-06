from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect

from vrm_solar_automation.db import create_engine_for_url, upgrade_database
from vrm_solar_automation.policy import PumpDecision, PumpPolicyState
from vrm_solar_automation.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_state_roundtrip_contains_runtime_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_url = _database_url(temp_dir)
            upgrade_database(database_url)
            with StateStore(database_url) as store:
                expected = PumpPolicyState(
                    is_on=True,
                    changed_at_iso=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                    quiet_hours_forced_off=True,
                    battery_alert_below_40_sent=True,
                    battery_alert_below_35_sent=True,
                    battery_alert_below_30_sent=False,
                    generator_running_alert_sent=True,
                    weather_cache_local_date="2026-01-01",
                    weather_cache_current_temperature_c=10.0,
                    weather_cache_today_min_temperature_c=8.0,
                    weather_cache_today_max_temperature_c=17.0,
                    weather_cache_today_sunshine_hours=6.5,
                    weather_cache_weather_code=3,
                    weather_cache_queried_timezone="Europe/Madrid",
                    weather_cache_cached_at_iso=datetime(2026, 1, 1, 3, tzinfo=UTC).isoformat(),
                    last_known_plug_is_on=False,
                    last_known_plug_at_iso=datetime(2026, 1, 1, 1, tzinfo=UTC).isoformat(),
                    last_actuation_error=None,
                    last_actuation_at_iso=datetime(2026, 1, 1, 2, tzinfo=UTC).isoformat(),
                )

                store.save(expected)
                loaded = store.load()

        self.assertEqual(loaded, expected)

    def test_record_control_cycle_persists_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_url = _database_url(temp_dir)
            upgrade_database(database_url)
            with StateStore(database_url) as store:
                store.record_control_cycle(
                    timestamp_unix_ms=1_711_000_000_000,
                    timestamp_iso=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                    power={
                        "site_id": 1,
                        "site_name": "Alaro",
                        "site_identifier": "cerbo-local",
                        "queried_at_unix_ms": 1_711_000_000_000,
                        "queried_at_iso": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                        "battery_soc_percent": 77.5,
                        "solar_watts": 2800.0,
                        "house_watts": 900.0,
                        "house_l1_watts": 400.0,
                        "house_l2_watts": 500.0,
                        "house_l3_watts": None,
                        "generator_watts": 0.0,
                        "active_input_source": 240,
                    },
                    weather={
                        "current_temperature_c": 10.0,
                        "today_min_temperature_c": 8.0,
                        "today_max_temperature_c": 17.0,
                        "today_sunshine_hours": 6.5,
                        "weather_code": 3,
                        "queried_timezone": "Europe/Madrid",
                    },
                    weather_source="live",
                    decision=PumpDecision(
                        should_turn_on=True,
                        action="turn_on",
                        reason="test",
                        reasons=["test"],
                        weather_mode="sufficient_sun",
                    ),
                    intended_target_is_on=True,
                    quiet_hours_blocked=False,
                    blocked_reason=None,
                    actuation={
                        "status": "reconciled",
                        "command_sent": "turn_on",
                        "observed_before_is_on": False,
                        "observed_after_is_on": True,
                        "error": None,
                    },
                )

            engine = create_engine_for_url(database_url)
            with engine.begin() as connection:
                row = connection.exec_driver_sql(
                    "SELECT site_identifier, decision_action, actuation_status, weather_source "
                    "FROM control_cycle"
                ).first()
            engine.dispose()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "cerbo-local")
        self.assertEqual(row[1], "turn_on")
        self.assertEqual(row[2], "reconciled")
        self.assertEqual(row[3], "live")

    def test_migration_upgrade_is_idempotent_and_creates_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_url = _database_url(temp_dir)
            upgrade_database(database_url)
            upgrade_database(database_url)

            engine = create_engine_for_url(database_url)
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            indexes = {index["name"] for index in inspector.get_indexes("control_cycle")}
            columns = {column["name"] for column in inspector.get_columns("controller_state")}
            control_cycle_columns = {
                column["name"] for column in inspector.get_columns("control_cycle")
            }
            engine.dispose()

        self.assertEqual(tables, {"alembic_version", "controller_state", "control_cycle"})
        self.assertIn("ix_control_cycle_timestamp_unix_ms", indexes)
        self.assertIn("ix_control_cycle_site_identifier_timestamp_unix_ms", indexes)
        self.assertIn("ix_control_cycle_command_sent_timestamp_unix_ms", indexes)
        self.assertIn("quiet_hours_forced_off", columns)
        self.assertIn("battery_alert_below_40_sent", columns)
        self.assertIn("battery_alert_below_35_sent", columns)
        self.assertIn("battery_alert_below_30_sent", columns)
        self.assertIn("generator_running_alert_sent", columns)
        self.assertIn("weather_cache_local_date", columns)
        self.assertIn("weather_cache_current_temperature_c", columns)
        self.assertIn("weather_cache_today_min_temperature_c", columns)
        self.assertIn("weather_cache_today_max_temperature_c", columns)
        self.assertIn("weather_cache_today_sunshine_hours", columns)
        self.assertIn("weather_cache_weather_code", columns)
        self.assertIn("weather_cache_queried_timezone", columns)
        self.assertIn("weather_cache_cached_at_iso", columns)
        self.assertIn("weather_source", control_cycle_columns)
        self.assertIn("today_sunshine_hours", control_cycle_columns)



def _database_url(temp_dir: str) -> str:
    return f"sqlite:///{Path(temp_dir) / 'automation.db'}"


if __name__ == "__main__":
    unittest.main()
