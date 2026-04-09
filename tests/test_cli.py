from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from vrm_solar_automation.cli import _run_control, build_parser
from vrm_solar_automation.policy import PumpDecision


class _FakeControlSystem:
    def __init__(self, settings) -> None:
        self._settings = settings

    async def control(self):
        decision = PumpDecision(
            should_turn_on=False,
            action="keep_off",
            reason="Cerbo telemetry is unavailable after retries, so the controller keeps the pump off.",
            reasons=["Cerbo telemetry is unavailable after retries."],
            weather_mode="telemetry_unavailable",
            soc_control_mode="telemetry_hold",
        )
        payload = {
            "power": {
                "battery_soc_percent": None,
                "solar_watts": None,
                "house_watts": None,
                "generator_watts": None,
            },
            "power_status": {
                "source": "cerbo_modbus",
                "available": False,
                "error": "Unable to reach Cerbo GX at cerbo.local:502. Details: timed out",
            },
            "weather": {
                "today_sunshine_hours": 10.0,
                "tomorrow_sunshine_hours": 11.0,
                "current_temperature_c": 21.0,
                "weather_code": 3,
            },
            "actuation": {
                "status": "no_target_change",
                "command_sent": None,
                "observed_after_is_on": False,
                "error": None,
            },
            "intended_target_is_on": False,
            "quiet_hours_blocked": False,
            "blocked_reason": None,
            "night_surplus_mode_active": False,
            "night_required_soc_percent": None,
            "night_reference_sunshine_hours": None,
            "effective_turn_on_soc_percent": None,
            "effective_turn_off_soc_percent": None,
            "forecast_liberal_factor": None,
            "soc_control_mode": "telemetry_hold",
        }
        return decision, payload


class CliTests(unittest.TestCase):
    def test_build_parser_accepts_control_command(self) -> None:
        args = build_parser().parse_args(["--env-file", "custom.env", "control", "--json"])

        self.assertEqual(args.env_file, "custom.env")
        self.assertEqual(args.command, "control")
        self.assertTrue(args.json)

    def test_build_parser_no_longer_exposes_override_commands(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["override-status"])

    def test_build_parser_accepts_db_upgrade_command(self) -> None:
        args = build_parser().parse_args(["db-upgrade"])
        self.assertEqual(args.command, "db-upgrade")


class CliOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_control_prints_power_telemetry_error_when_unavailable(self) -> None:
        with (
            patch("vrm_solar_automation.cli.load_settings", return_value=object()),
            patch("vrm_solar_automation.cli.PumpControlSystem", _FakeControlSystem),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = await _run_control(".env", False)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("Power telemetry: unavailable", output)
        self.assertIn("timed out", output)


if __name__ == "__main__":
    unittest.main()
