from __future__ import annotations

import unittest
from datetime import UTC, datetime
import tempfile
from pathlib import Path

import aiohttp

from vrm_solar_automation.db import create_engine_for_url, upgrade_database
from vrm_solar_automation.config import Settings
from vrm_solar_automation.models import PowerSnapshot
from vrm_solar_automation.policy import PumpPolicy, PumpPolicyState
from vrm_solar_automation.shelly import (
    ShellyError,
    ShellySwitchCommandResult,
    ShellySwitchStatus,
)
from vrm_solar_automation.state import StateStore
from vrm_solar_automation.system import PumpControlSystem
from vrm_solar_automation.weather import WeatherSnapshot


class FakeProbeClient:
    source = "cerbo_modbus"

    def __init__(
        self,
        snapshot: PowerSnapshot | Exception | list[PowerSnapshot | Exception],
    ) -> None:
        if isinstance(snapshot, list):
            self._snapshots = list(snapshot)
        else:
            self._snapshots = [snapshot]
        self.fetch_calls = 0

    async def fetch_snapshot(self) -> PowerSnapshot:
        self.fetch_calls += 1
        if len(self._snapshots) == 1:
            current = self._snapshots[0]
        else:
            current = self._snapshots.pop(0)
        if isinstance(current, Exception):
            raise current
        return current


class FakeWeatherClient:
    def __init__(self, snapshot: WeatherSnapshot) -> None:
        self._snapshot = snapshot

    async def fetch_weather(self, **kwargs) -> WeatherSnapshot:
        return self._snapshot


class FakeCountingWeatherClient:
    def __init__(self, snapshots: list[WeatherSnapshot], *, fail_after: int | None = None) -> None:
        self._snapshots = snapshots
        self._fail_after = fail_after
        self.fetch_count = 0

    async def fetch_weather(self, **kwargs) -> WeatherSnapshot:
        self.fetch_count += 1
        if self._fail_after is not None and self.fetch_count > self._fail_after:
            raise aiohttp.ClientConnectionError("weather unavailable")
        if self.fetch_count <= len(self._snapshots):
            return self._snapshots[self.fetch_count - 1]
        return self._snapshots[-1]


class FakeAlwaysFailWeatherClient:
    async def fetch_weather(self, **kwargs) -> WeatherSnapshot:
        raise aiohttp.ClientConnectionError("weather unavailable")


class FakeUnavailableProbeClient:
    source = "cerbo_modbus"

    async def fetch_snapshot(self) -> PowerSnapshot:
        raise TimeoutError("timed out")


class FakePlugClient:
    def __init__(
        self,
        status_outputs: list[bool] | None = None,
        *,
        turn_on_output: bool = True,
        turn_off_output: bool = False,
    ) -> None:
        raw_outputs = status_outputs or [False, True]
        self._status_reads = [_build_switch_status(output) for output in raw_outputs]
        self._turn_on_output = turn_on_output
        self._turn_off_output = turn_off_output
        self.turn_on_calls = 0
        self.turn_off_calls = 0

    async def fetch_switch_status(self) -> ShellySwitchStatus:
        if len(self._status_reads) == 1:
            return self._status_reads[0]
        return self._status_reads.pop(0)

    async def turn_on(self) -> ShellySwitchCommandResult:
        self.turn_on_calls += 1
        return ShellySwitchCommandResult(
            switch_id=0,
            requested_on=True,
            was_on=False,
            output=self._turn_on_output,
            source="HTTP_in",
            toggle_after_seconds=None,
            executed_at_iso=datetime.now(UTC).isoformat(),
        )

    async def turn_off(self) -> ShellySwitchCommandResult:
        self.turn_off_calls += 1
        return ShellySwitchCommandResult(
            switch_id=0,
            requested_on=False,
            was_on=True,
            output=self._turn_off_output,
            source="HTTP_in",
            toggle_after_seconds=None,
            executed_at_iso=datetime.now(UTC).isoformat(),
        )


class FakeStateStore:
    def __init__(self) -> None:
        self.state = None
        self.cycles: list[dict[str, object]] = []

    def load(self):
        return self.state

    def save(self, state) -> None:
        self.state = state

    def record_control_cycle(self, **kwargs) -> None:
        self.cycles.append(kwargs)


class UnreachablePlugClient:
    """Stands in for the July outage: every plug read fails."""

    async def fetch_switch_status(self) -> ShellySwitchStatus:
        raise ShellyError("plug unreachable")

    async def turn_on(self) -> ShellySwitchCommandResult:
        raise ShellyError("plug unreachable")

    async def turn_off(self) -> ShellySwitchCommandResult:
        raise ShellyError("plug unreachable")


class FakeNotifier:
    def __init__(self, *, should_raise: bool = False) -> None:
        self.should_raise = should_raise
        self.calls: list[dict[str, object]] = []
        self.mismatch_alert_calls: list[dict[str, object]] = []
        self.battery_alert_calls: list[dict[str, object]] = []
        self.generator_alert_calls: list[dict[str, object]] = []
        self.weather_block_alert_calls: list[dict[str, object]] = []

    def send_plug_state_change_email(
        self,
        *,
        command_sent: str,
        decision_action: str,
        decision_reason: str,
        intended_is_on: bool,
        actuation_status: str,
        observed_before_is_on: bool | None,
        observed_after_is_on: bool | None,
        at_iso: str,
    ) -> None:
        self.calls.append(
            {
                "command_sent": command_sent,
                "decision_action": decision_action,
                "decision_reason": decision_reason,
                "intended_is_on": intended_is_on,
                "actuation_status": actuation_status,
                "observed_before_is_on": observed_before_is_on,
                "observed_after_is_on": observed_after_is_on,
                "at_iso": at_iso,
            }
        )
        if self.should_raise:
            raise RuntimeError("smtp failed")

    def send_plug_state_mismatch_email(
        self,
        *,
        at_iso: str,
        intended_is_on: bool,
        observed_is_on: bool,
        decision_action: str,
        decision_reason: str,
        actuation_status: str,
    ) -> None:
        self.mismatch_alert_calls.append(
            {
                "at_iso": at_iso,
                "intended_is_on": intended_is_on,
                "observed_is_on": observed_is_on,
                "decision_action": decision_action,
                "decision_reason": decision_reason,
                "actuation_status": actuation_status,
            }
        )
        if self.should_raise:
            raise RuntimeError("smtp failed")

    def send_battery_alert_email(
        self,
        *,
        battery_soc_percent: float,
        crossed_thresholds: tuple[int, ...],
        at_iso: str,
    ) -> None:
        self.battery_alert_calls.append(
            {
                "battery_soc_percent": battery_soc_percent,
                "crossed_thresholds": crossed_thresholds,
                "at_iso": at_iso,
            }
        )
        if self.should_raise:
            raise RuntimeError("smtp failed")

    def send_generator_started_email(
        self,
        *,
        generator_watts: float,
        at_iso: str,
    ) -> None:
        self.generator_alert_calls.append(
            {
                "generator_watts": generator_watts,
                "at_iso": at_iso,
            }
        )
        if self.should_raise:
            raise RuntimeError("smtp failed")

    def send_weather_blocked_alert_email(
        self,
        *,
        at_iso: str,
        local_date: str,
        weather_mode: str,
        decision_reason: str,
        today_sunshine_hours: float | None,
        tomorrow_sunshine_hours: float | None,
        night_reference_sunshine_hours: float | None,
    ) -> None:
        self.weather_block_alert_calls.append(
            {
                "at_iso": at_iso,
                "local_date": local_date,
                "weather_mode": weather_mode,
                "decision_reason": decision_reason,
                "today_sunshine_hours": today_sunshine_hours,
                "tomorrow_sunshine_hours": tomorrow_sunshine_hours,
                "night_reference_sunshine_hours": night_reference_sunshine_hours,
            }
        )
        if self.should_raise:
            raise RuntimeError("smtp failed")


class PumpPolicyAndControlTests(unittest.IsolatedAsyncioTestCase):
    def test_generator_power_does_not_block_operation(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=1200.0, battery_soc_percent=82.0),
            weather=_build_sunny_weather(),
            previous_state=None,
        )

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_on")
        self.assertNotIn("Generator", decision.reason)

    def test_generator_power_does_not_override_low_soc_cutoff(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=1200.0, battery_soc_percent=22.0),
            weather=_build_sunny_weather(),
            previous_state=None,
        )

        self.assertFalse(decision.should_turn_on)
        self.assertIn("hard automatic cutoff", decision.reason)

    def test_weather_unknown_keeps_operation_off(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0),
            weather=WeatherSnapshot(
                current_temperature_c=None,
                today_min_temperature_c=None,
                today_max_temperature_c=None,
                today_sunshine_hours=None,
                weather_code=None,
                queried_timezone="Europe/Madrid",
            ),
            previous_state=None,
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.weather_mode, "unknown")
        self.assertIn("sunshine-hours forecast is unavailable", decision.reason)

    def test_insufficient_sun_keeps_operation_off(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0),
            weather=_build_sunny_weather(today_sunshine_hours=3.5),
            previous_state=None,
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.weather_mode, "insufficient_sun")
        self.assertIn("below the 6.5-hour minimum", decision.reason)

    def test_hard_soc_cutoff_keeps_operation_off(self) -> None:
        previous_state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=22.0),
            weather=_build_sunny_weather(),
            previous_state=previous_state,
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_off")
        self.assertIn("at or below the 22.5% hard automatic cutoff", decision.reason)

    async def test_control_uses_configured_soc_threshold(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                battery_min_soc_percent=45.0,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=46.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather(today_sunshine_hours=9.0)),
            state_store=FakeStateStore(),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["next_state"]["is_on"])
        self.assertIn("meeting the adaptive 45.0% turn-on threshold", decision.reason)

    async def test_morning_gets_no_keep_running_bias(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, 8, 29, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(
                battery_min_soc_percent=45.0,
                auto_off_start_local="18:30",
                auto_resume_start_local="08:30",
                auto_control_timezone="UTC",
                surplus_night_enabled=False,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=40.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=10.5, tomorrow_sunshine_hours=10.5)
            ),
            state_store=state_store,
            now_provider=_fixed_now(2026, 1, 10, 8, 31),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_off")
        self.assertAlmostEqual(float(payload["effective_turn_off_soc_percent"]), 40.0, places=1)
        self.assertAlmostEqual(float(payload["forecast_liberal_factor"]), 0.5, places=2)

    async def test_morning_resume_strong_forecast_can_restart_below_conservative_cutoff(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                battery_min_soc_percent=45.0,
                auto_resume_start_local="08:30",
                auto_control_timezone="UTC",
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=43.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=10.5, tomorrow_sunshine_hours=10.5)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 8, 45),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertAlmostEqual(float(payload["effective_turn_on_soc_percent"]), 42.5, places=1)
        self.assertIn("meeting the adaptive 42.5% turn-on threshold", decision.reason)

    async def test_morning_resume_weak_forecast_keeps_conservative_daytime_cutoff(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, 8, 29, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(
                battery_min_soc_percent=45.0,
                auto_resume_start_local="08:30",
                auto_control_timezone="UTC",
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=40.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather(today_sunshine_hours=9.0)),
            state_store=state_store,
            now_provider=_fixed_now(2026, 1, 10, 8, 31),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertAlmostEqual(float(payload["effective_turn_off_soc_percent"]), 45.0, places=1)
        self.assertIn("needs at least 45.0% SOC to keep running", decision.reason)

    async def test_daytime_keep_running_threshold_is_time_independent(self) -> None:
        thresholds = []
        for hour, minute in ((8, 45), (11, 1), (16, 0)):
            state_store = FakeStateStore()
            state_store.state = PumpPolicyState(
                is_on=True,
                changed_at_iso=datetime(2026, 1, 10, 8, 29, tzinfo=UTC).isoformat(),
            )
            system = PumpControlSystem(
                _test_settings(
                    battery_min_soc_percent=45.0,
                    auto_off_start_local="18:30",
                    auto_resume_start_local="08:30",
                    auto_control_timezone="UTC",
                    surplus_night_enabled=False,
                ),
                probe_client=FakeProbeClient(
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=41.0)
                ),
                weather_client=FakeWeatherClient(
                    _build_sunny_weather(today_sunshine_hours=10.5, tomorrow_sunshine_hours=10.5)
                ),
                state_store=state_store,
                now_provider=_fixed_now(2026, 1, 10, hour, minute),
            )

            decision, payload = await system.evaluate()

            self.assertTrue(decision.should_turn_on)
            thresholds.append(float(payload["effective_turn_off_soc_percent"]))

        self.assertEqual(thresholds, [40.0, 40.0, 40.0])

    async def test_daytime_hard_cutoff_overrides_strong_forecast(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                battery_min_soc_percent=45.0,
                auto_resume_start_local="08:30",
                auto_control_timezone="UTC",
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=22.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=12.0, tomorrow_sunshine_hours=12.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 9, 0),
        )

        decision, _ = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertIn("22.5% hard automatic cutoff", decision.reason)

    async def test_daytime_uses_weaker_tomorrow_forecast_for_soc_thresholds(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                battery_min_soc_percent=45.0,
                auto_resume_start_local="08:30",
                auto_control_timezone="UTC",
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=43.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=10.5, tomorrow_sunshine_hours=9.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 8, 45),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertAlmostEqual(float(payload["effective_turn_on_soc_percent"]), 45.0, places=1)
        self.assertAlmostEqual(float(payload["forecast_liberal_factor"]), 0.0, places=2)
        self.assertIn("tomorrow's weaker 9.0-hour sunshine forecast", decision.reason)

    async def test_daytime_falls_back_to_today_when_tomorrow_forecast_is_missing(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                battery_min_soc_percent=45.0,
                auto_resume_start_local="08:30",
                auto_control_timezone="UTC",
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=43.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=10.5, tomorrow_sunshine_hours=None)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 8, 45),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertAlmostEqual(float(payload["effective_turn_on_soc_percent"]), 42.5, places=1)
        self.assertAlmostEqual(float(payload["forecast_liberal_factor"]), 0.5, places=2)
        self.assertNotIn("tomorrow's weaker", decision.reason)

    async def test_control_uses_configured_sunshine_threshold(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                sunshine_hours_min=6.5,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather(today_sunshine_hours=6.0)),
            state_store=FakeStateStore(),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertFalse(payload["next_state"]["is_on"])
        self.assertIn("below the 6.5-hour minimum", decision.reason)

    async def test_control_applies_shelly_command_when_target_changes(self) -> None:
        state_store = FakeStateStore()
        notifier = FakeNotifier()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(),
            state_store=state_store,
            notifier=notifier,
        )

        decision, payload = await system.control()

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(payload["weather_source"], "live")
        self.assertEqual(payload["actuation"]["status"], "reconciled")
        self.assertEqual(payload["actuation"]["command_sent"], "turn_on")
        self.assertTrue(payload["next_state"]["last_known_plug_is_on"])
        self.assertIsNotNone(payload["next_state"]["weather_cache_local_date"])
        self.assertNotIn("override", payload)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0]["command_sent"], "turn_on")
        self.assertEqual(len(state_store.cycles), 1)
        self.assertEqual(notifier.calls[0]["decision_action"], decision.action)
        self.assertEqual(notifier.calls[0]["decision_reason"], decision.reason)
        self.assertTrue(notifier.calls[0]["intended_is_on"])

    async def test_evaluate_is_read_only(self) -> None:
        state_store = FakeStateStore()
        previous_state = PumpPolicyState(
            is_on=False,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        state_store.state = previous_state
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=state_store,
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["next_state"]["is_on"])
        self.assertFalse(payload["quiet_hours_blocked"])
        self.assertIs(state_store.state, previous_state)

    async def test_evaluate_gracefully_degrades_when_cerbo_is_unavailable(self) -> None:
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeUnavailableProbeClient(),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=FakeStateStore(),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(payload["power_status"]["available"])
        self.assertIn("Unable to reach Cerbo GX", payload["power_status"]["error"])
        self.assertIn("timed out", payload["power_status"]["error"])
        self.assertIsNone(payload["power"]["battery_soc_percent"])
        self.assertFalse(decision.should_turn_on)
        self.assertIn("Cerbo telemetry is unavailable after retries", payload["decision"]["reason"])
        self.assertEqual(payload["soc_control_mode"], "telemetry_hold")
        self.assertEqual(payload["next_state"]["consecutive_power_failures"], 1)

    async def test_power_fetch_retry_recovers_within_same_cycle(self) -> None:
        probe_client = FakeProbeClient(
            [
                TimeoutError("transient timeout"),
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
            ]
        )
        system = PumpControlSystem(
            _test_settings(),
            probe_client=probe_client,
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=FakeStateStore(),
        )

        decision, payload = await system.control()

        self.assertEqual(probe_client.fetch_calls, 2)
        self.assertTrue(payload["power_status"]["available"])
        self.assertTrue(decision.should_turn_on)
        self.assertEqual(payload["next_state"]["consecutive_power_failures"], 0)

    async def test_first_failed_cycle_holds_previous_on_target(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=True,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(TimeoutError("timed out")),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(status_outputs=[True]),
            state_store=state_store,
        )

        decision, payload = await system.control()

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(decision.action, "keep_on")
        self.assertEqual(payload["soc_control_mode"], "telemetry_hold")
        self.assertEqual(payload["actuation"]["status"], "no_target_change")
        self.assertIsNone(payload["actuation"]["command_sent"])
        self.assertEqual(payload["next_state"]["consecutive_power_failures"], 1)
        self.assertIn("holding the previous automatic ON target (failure 1/3)", decision.reason)

    async def test_third_consecutive_failed_cycle_forces_off(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            consecutive_power_failures=0,
            last_known_plug_is_on=True,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(TimeoutError("timed out")),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(status_outputs=[True, True, True, False]),
            state_store=state_store,
        )

        first_decision, first_payload = await system.control()
        second_decision, second_payload = await system.control()
        third_decision, third_payload = await system.control()

        self.assertTrue(first_decision.should_turn_on)
        self.assertTrue(second_decision.should_turn_on)
        self.assertFalse(third_decision.should_turn_on)
        self.assertEqual(first_payload["next_state"]["consecutive_power_failures"], 1)
        self.assertEqual(second_payload["next_state"]["consecutive_power_failures"], 2)
        self.assertEqual(third_payload["next_state"]["consecutive_power_failures"], 3)
        self.assertEqual(third_payload["actuation"]["status"], "reconciled")
        self.assertEqual(third_payload["actuation"]["command_sent"], "turn_off")
        self.assertIn("failure count reached 3/3", third_decision.reason)

    async def test_failed_cycle_with_previous_off_state_stays_off(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=False,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=False,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(TimeoutError("timed out")),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(status_outputs=[False]),
            state_store=state_store,
        )

        decision, payload = await system.control()

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "keep_off")
        self.assertEqual(payload["actuation"]["status"], "no_target_change")
        self.assertIsNone(payload["actuation"]["command_sent"])
        self.assertEqual(payload["next_state"]["consecutive_power_failures"], 1)

    async def test_successful_cycle_resets_failure_streak(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            consecutive_power_failures=2,
            last_power_failure_at_iso=datetime(2026, 1, 10, 10, tzinfo=UTC).isoformat(),
            last_power_failure_error="timed out",
            last_known_plug_is_on=True,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(status_outputs=[True]),
            state_store=state_store,
        )

        decision, payload = await system.control()

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(payload["next_state"]["consecutive_power_failures"], 0)
        self.assertEqual(payload["next_state"]["last_power_failure_error"], "timed out")

    async def test_evaluate_uses_mock_cerbo_snapshot_when_enabled(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                state_file=".state/test-state.json",
                cerbo_mock_enabled=True,
                cerbo_site_name="Mock Cerbo GX",
                cerbo_site_identifier="cerbo-mock",
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=FakeStateStore(),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(payload["power_status"]["available"])
        self.assertEqual(payload["power_status"]["source"], "cerbo_mock")
        self.assertEqual(payload["power"]["site_name"], "Mock Cerbo GX")
        self.assertEqual(payload["power"]["site_identifier"], "cerbo-mock")
        self.assertEqual(payload["power"]["battery_soc_percent"], 78.0)
        self.assertEqual(payload["power"]["solar_watts"], 2850.0)
        self.assertEqual(payload["power"]["house_watts"], 940.0)
        self.assertEqual(payload["power"]["generator_watts"], 0.0)
        self.assertTrue(decision.should_turn_on)

    async def test_quiet_hours_block_automatic_on_commands(self) -> None:
        notifier = FakeNotifier()
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="00:00",
                auto_resume_start_local="23:59",
                surplus_night_enabled=False,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(status_outputs=[False, False]),
            state_store=FakeStateStore(),
            notifier=notifier,
        )

        decision, payload = await system.control()

        self.assertTrue(decision.should_turn_on)
        self.assertFalse(payload["intended_target_is_on"])
        self.assertTrue(payload["quiet_hours_blocked"])
        self.assertEqual(payload["actuation"]["status"], "blocked_quiet_hours")
        self.assertIsNone(payload["actuation"]["command_sent"])
        self.assertEqual(notifier.calls, [])

    async def test_quiet_hours_force_running_plug_off(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, 18, 0, tzinfo=UTC).isoformat(),
            quiet_hours_forced_off=False,
            last_known_plug_is_on=True,
            last_known_plug_at_iso=datetime(2026, 1, 10, 18, 0, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:30",
                auto_resume_start_local="08:30",
                surplus_night_enabled=False,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(status_outputs=[True, False]),
            state_store=state_store,
            now_provider=_fixed_now(2026, 1, 10, 18, 31),
        )

        decision, payload = await system.control()

        self.assertTrue(decision.should_turn_on)
        self.assertFalse(payload["intended_target_is_on"])
        self.assertTrue(payload["quiet_hours_blocked"])
        self.assertEqual(payload["actuation"]["status"], "reconciled")
        self.assertEqual(payload["actuation"]["command_sent"], "turn_off")
        self.assertTrue(payload["next_state"]["quiet_hours_forced_off"])

    async def test_quiet_hours_resume_turns_plug_back_on(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, 18, 0, tzinfo=UTC).isoformat(),
            quiet_hours_forced_off=True,
            last_known_plug_is_on=False,
            last_known_plug_at_iso=datetime(2026, 1, 10, 18, 31, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:30",
                auto_resume_start_local="08:30",
                surplus_night_enabled=False,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(status_outputs=[False, True]),
            state_store=state_store,
            now_provider=_fixed_now(2026, 1, 11, 8, 31),
        )

        decision, payload = await system.control()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["intended_target_is_on"])
        self.assertFalse(payload["quiet_hours_blocked"])
        self.assertEqual(payload["actuation"]["status"], "reconciled")
        self.assertEqual(payload["actuation"]["command_sent"], "turn_on")
        self.assertFalse(payload["next_state"]["quiet_hours_forced_off"])

    async def test_quiet_hours_wrap_overnight_with_fixed_schedule(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="17:30",
                auto_resume_start_local="09:00",
                surplus_night_enabled=False,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 18, 0),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertFalse(payload["intended_target_is_on"])
        self.assertTrue(payload["quiet_hours_blocked"])

    async def test_surplus_night_turns_on_after_evening_with_sunny_tomorrow(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=85.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=6.0, tomorrow_sunshine_hours=10.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 19, 0),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(decision.weather_mode, "surplus_night")
        self.assertTrue(payload["intended_target_is_on"])
        self.assertFalse(payload["quiet_hours_blocked"])
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertAlmostEqual(float(payload["night_required_soc_percent"]), 70.9, places=1)
        self.assertAlmostEqual(float(payload["night_reference_sunshine_hours"]), 10.0, places=1)

    async def test_surplus_night_stays_off_when_tomorrow_is_not_sunny_enough(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=95.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=6.0, tomorrow_sunshine_hours=8.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 19, 0),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertFalse(payload["intended_target_is_on"])
        self.assertIn("below the 9.0-hour surplus-night minimum", decision.reason)

    async def test_surplus_night_stays_off_when_evening_soc_is_below_turn_on_threshold(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=70.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=6.0, tomorrow_sunshine_hours=10.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 19, 0),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertFalse(payload["intended_target_is_on"])
        self.assertIn("needs at least 84.1% SOC to turn on", decision.reason)

    async def test_surplus_night_forced_off_window_blocks_pump_without_excess_soc(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=10.0, tomorrow_sunshine_hours=11.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 11, 2, 0),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertTrue(payload["night_forced_off_window_active"])
        self.assertFalse(payload["intended_target_is_on"])
        self.assertAlmostEqual(float(payload["night_required_soc_percent"]), 49.9, places=1)
        self.assertAlmostEqual(float(payload["night_pump_reserve_soc_percent"]), 4.2, places=1)
        self.assertIn("forced-off window needs at least 63.1% SOC to turn on", decision.reason)

    async def test_surplus_night_forced_off_window_allows_pump_with_excess_soc(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=75.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=10.0, tomorrow_sunshine_hours=11.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 11, 2, 0),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertTrue(payload["night_forced_off_window_active"])
        self.assertTrue(payload["intended_target_is_on"])
        self.assertIn("forced-off window", decision.reason)
        self.assertIn("meeting the 63.1% turn-on threshold", decision.reason)

    async def test_surplus_night_runs_normally_before_midnight(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=85.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=6.0, tomorrow_sunshine_hours=10.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 23, 0),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertFalse(payload["night_forced_off_window_active"])
        self.assertAlmostEqual(float(payload["night_required_soc_percent"]), 58.9, places=1)
        self.assertAlmostEqual(float(payload["night_pump_reserve_soc_percent"]), 4.2, places=1)

    async def test_surplus_night_forced_off_window_starts_at_11_30pm(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=6.0, tomorrow_sunshine_hours=10.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 23, 45),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertTrue(payload["night_forced_off_window_active"])
        self.assertAlmostEqual(float(payload["night_pump_reserve_soc_percent"]), 4.2, places=1)
        self.assertIn("forced-off window", decision.reason)

    async def test_surplus_night_resumes_normally_after_forced_off_window(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=10.0, tomorrow_sunshine_hours=11.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 11, 5, 0),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertFalse(payload["night_forced_off_window_active"])
        self.assertAlmostEqual(float(payload["night_required_soc_percent"]), 40.9, places=1)
        self.assertAlmostEqual(float(payload["night_pump_reserve_soc_percent"]), 4.2, places=1)

    async def test_surplus_night_reserve_does_not_decay_toward_the_floor_before_dawn(self) -> None:
        """The pre-dawn hours must be the least permissive part of the night, not the most.

        The retired formula counted down to AUTO_RESUME_START_LOCAL, so its requirement fell
        to the floor as dawn approached and cleared the pump to start at 03:30 and run into
        the morning. Anchoring on solar crossover keeps a real morning budget in reserve.
        """
        requirements = []
        for hour in (23, 1, 3, 5):
            system = PumpControlSystem(
                _test_settings(
                    auto_off_start_local="18:00",
                    auto_resume_start_local="08:00",
                    surplus_night_enabled=True,
                ),
                probe_client=FakeProbeClient(
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0)
                ),
                weather_client=FakeWeatherClient(
                    _build_sunny_weather(today_sunshine_hours=10.0, tomorrow_sunshine_hours=10.0)
                ),
                state_store=FakeStateStore(),
                now_provider=_fixed_now(2026, 1, 11, hour, 0),
            )
            _, payload = await system.evaluate()
            requirements.append(float(payload["night_required_soc_percent"]))

        # Still falls as the night is used up, but never below the morning budget: from 06:00
        # to the 08:15 crossover the house draws 1.75 kW, which is 7.9% of a 50 kWh battery.
        self.assertEqual(requirements, sorted(requirements, reverse=True))
        self.assertGreater(requirements[-1], 40.0)
        self.assertAlmostEqual(requirements[-1], 40.875, places=2)

    async def test_surplus_night_start_is_dearer_than_continuing(self) -> None:
        """Starting carries the compressor run it commits to; continuing does not."""
        settings = _test_settings(
            auto_off_start_local="18:00",
            auto_resume_start_local="08:00",
            surplus_night_enabled=True,
        )
        weather = _build_sunny_weather(today_sunshine_hours=10.0, tomorrow_sunshine_hours=10.0)

        def _system(previous_is_on: bool) -> PumpControlSystem:
            state_store = FakeStateStore()
            if previous_is_on:
                state_store.state = PumpPolicyState(
                    is_on=True,
                    changed_at_iso=datetime(2026, 1, 10, 20, 0, tzinfo=UTC).isoformat(),
                )
            return PumpControlSystem(
                settings,
                probe_client=FakeProbeClient(
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=52.0)
                ),
                weather_client=FakeWeatherClient(weather),
                state_store=state_store,
                now_provider=_fixed_now(2026, 1, 11, 5, 0),
            )

        running, running_payload = await _system(True).evaluate()
        stopped, stopped_payload = await _system(False).evaluate()

        # 52% clears the 40.9% keep-running threshold but not the 54.1% turn-on threshold.
        self.assertTrue(running.should_turn_on)
        self.assertFalse(stopped.should_turn_on)
        self.assertGreater(
            float(stopped_payload["effective_turn_on_soc_percent"]),
            float(running_payload["effective_turn_off_soc_percent"]),
        )
        self.assertAlmostEqual(float(stopped_payload["night_pump_reserve_soc_percent"]), 4.2, places=1)

    async def test_surplus_night_reserve_covers_the_gap_to_solar_crossover(self) -> None:
        """The budget must run to measured crossover, not to the daytime-control resume time."""
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_solar_crossover_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=10.0, tomorrow_sunshine_hours=10.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 11, 5, 0),
        )

        _, at_resume_time = await system.evaluate()

        # Pulling crossover back to the 08:00 resume time drops a quarter hour of morning
        # base load: 0.25 h x 1.75 kW over 50 kWh is 0.875%.
        self.assertAlmostEqual(float(at_resume_time["night_required_soc_percent"]), 40.0, places=2)

    async def test_surplus_night_uses_hysteresis_to_keep_running_until_off_threshold(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, 18, 30, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=75.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=6.0, tomorrow_sunshine_hours=10.0)
            ),
            state_store=state_store,
            now_provider=_fixed_now(2026, 1, 10, 19, 0),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(decision.action, "keep_on")
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertIn("above the 70.9% keep-running threshold", decision.reason)

    async def test_surplus_night_turns_off_when_soc_reaches_off_threshold(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, 18, 30, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=61.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=6.0, tomorrow_sunshine_hours=10.0)
            ),
            state_store=state_store,
            now_provider=_fixed_now(2026, 1, 10, 19, 0),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_off")
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertIn("needs at least 70.9% SOC to keep running", decision.reason)

    async def test_surplus_night_ignores_generator_power(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                auto_off_start_local="18:00",
                auto_resume_start_local="08:00",
                surplus_night_enabled=True,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=1200.0, battery_soc_percent=95.0)
            ),
            weather_client=FakeWeatherClient(
                _build_sunny_weather(today_sunshine_hours=6.0, tomorrow_sunshine_hours=10.0)
            ),
            state_store=FakeStateStore(),
            now_provider=_fixed_now(2026, 1, 10, 19, 0),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["night_surplus_mode_active"])
        self.assertNotIn("Generator", decision.reason)

    async def test_cycle_records_battery_power_and_policy_fingerprint(self) -> None:
        state_store = FakeStateStore()
        system = PumpControlSystem(
            _test_settings(battery_capacity_kwh=50.0, surplus_night_base_load_kw=1.5),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_power_w=-1750.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=state_store,
            plug_client=FakePlugClient([True]),
        )

        await system.control()

        cycle = state_store.cycles[-1]
        self.assertEqual(cycle["power"]["battery_power_w"], -1750.0)
        self.assertEqual(
            cycle["policy_fingerprint"],
            {
                "battery_capacity_kwh": 50.0,
                "night_base_load_kw": 1.5,
                "night_pump_load_kw": 2.1,
                "battery_hard_min_soc_percent": 22.5,
            },
        )

    async def test_plug_observed_state_is_recorded_without_a_target_change(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=state_store,
            plug_client=FakePlugClient([True]),
        )

        _, payload = await system.control()

        self.assertEqual(payload["actuation"]["status"], "no_target_change")
        self.assertIs(state_store.cycles[-1]["plug_observed_is_on"], True)

    async def test_plug_observed_state_is_null_when_plug_is_unreachable(self) -> None:
        state_store = FakeStateStore()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=state_store,
            plug_client=UnreachablePlugClient(),
        )

        await system.control()

        self.assertIsNone(state_store.cycles[-1]["plug_observed_is_on"])

    async def test_reachable_mismatch_is_reconciled_even_without_target_change(self) -> None:
        notifier = FakeNotifier()
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=False,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        plug_client = FakePlugClient(status_outputs=[False, True])
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=plug_client,
            state_store=state_store,
            notifier=notifier,
        )

        _, payload = await system.control()

        self.assertEqual(payload["actuation"]["status"], "reconciled")
        self.assertEqual(payload["actuation"]["command_sent"], "turn_on")
        self.assertEqual(plug_client.turn_on_calls, 1)
        self.assertEqual(len(notifier.calls), 0)
        self.assertEqual(notifier.mismatch_alert_calls, [])
        self.assertFalse(state_store.state.plug_mismatch_alert_sent)

    async def test_email_failure_is_non_blocking(self) -> None:
        notifier = FakeNotifier(should_raise=True)
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(),
            state_store=FakeStateStore(),
            notifier=notifier,
        )

        decision, payload = await system.control()

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(payload["actuation"]["status"], "reconciled")
        self.assertEqual(payload["actuation"]["command_sent"], "turn_on")
        self.assertIsNone(payload["actuation"]["error"])
        self.assertEqual(len(notifier.calls), 1)

    async def test_mismatch_alert_latch_resets_after_alignment(self) -> None:
        notifier = FakeNotifier()
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=False,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        plug_client = FakePlugClient(
            status_outputs=[False, False, False, False, True, False, False],
            turn_on_output=False,
        )
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=plug_client,
            state_store=state_store,
            notifier=notifier,
        )

        _, first_payload = await system.control()
        _, second_payload = await system.control()
        _, third_payload = await system.control()
        _, fourth_payload = await system.control()

        self.assertEqual(first_payload["actuation"]["status"], "mismatch_after_command")
        self.assertEqual(second_payload["actuation"]["status"], "mismatch_after_command")
        self.assertEqual(third_payload["actuation"]["status"], "no_target_change")
        self.assertEqual(fourth_payload["actuation"]["status"], "mismatch_after_command")
        self.assertEqual(plug_client.turn_on_calls, 3)
        self.assertEqual(len(notifier.mismatch_alert_calls), 2)
        self.assertTrue(state_store.state.plug_mismatch_alert_sent)

    async def test_generator_alert_is_sent_once_per_running_period(self) -> None:
        notifier = FakeNotifier()
        state_store = FakeStateStore()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(generator_watts=1200.0),
                    _build_power_snapshot(generator_watts=900.0),
                    _build_power_snapshot(generator_watts=0.0),
                    _build_power_snapshot(generator_watts=1500.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=state_store,
            notifier=notifier,
        )

        await system.control()
        await system.control()
        await system.control()
        _, payload = await system.control()

        self.assertEqual(len(notifier.generator_alert_calls), 2)
        self.assertEqual(notifier.generator_alert_calls[0]["generator_watts"], 1200.0)
        self.assertEqual(notifier.generator_alert_calls[1]["generator_watts"], 1500.0)
        self.assertTrue(payload["next_state"]["generator_running_alert_sent"])

    async def test_battery_alerts_fire_once_per_threshold_while_soc_keeps_falling(self) -> None:
        notifier = FakeNotifier()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(battery_soc_percent=36.0),
                    _build_power_snapshot(battery_soc_percent=35.0),
                    _build_power_snapshot(battery_soc_percent=33.0),
                    _build_power_snapshot(battery_soc_percent=30.0),
                    _build_power_snapshot(battery_soc_percent=26.0),
                    _build_power_snapshot(battery_soc_percent=25.0),
                    _build_power_snapshot(battery_soc_percent=23.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=FakeStateStore(),
            notifier=notifier,
        )

        payloads = [(await system.control())[1] for _ in range(7)]

        self.assertEqual(
            [call["crossed_thresholds"] for call in notifier.battery_alert_calls],
            [(35.0,), (25.0,)],
        )
        self.assertEqual(
            [call["battery_soc_percent"] for call in notifier.battery_alert_calls],
            [35.0, 25.0],
        )
        self.assertEqual(payloads[0]["next_state"]["battery_alert_latched_percents"], [])
        self.assertEqual(payloads[2]["next_state"]["battery_alert_latched_percents"], [35.0])
        self.assertEqual(
            payloads[-1]["next_state"]["battery_alert_latched_percents"], [35.0, 25.0]
        )

    async def test_battery_alert_latches_survive_a_telemetry_gap(self) -> None:
        notifier = FakeNotifier()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(battery_soc_percent=34.0),
                    TimeoutError("cerbo timed out"),
                    TimeoutError("cerbo timed out"),
                    TimeoutError("cerbo timed out"),
                    _build_power_snapshot(battery_soc_percent=33.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=FakeStateStore(),
            notifier=notifier,
        )

        await system.control()
        _, gap_payload = await system.control()
        _, recovered_payload = await system.control()

        self.assertFalse(gap_payload["power_status"]["available"])
        self.assertEqual(
            [call["crossed_thresholds"] for call in notifier.battery_alert_calls],
            [(35.0,)],
        )
        self.assertEqual(gap_payload["next_state"]["battery_alert_latched_percents"], [35.0])
        self.assertEqual(recovered_payload["next_state"]["battery_alert_latched_percents"], [35.0])

    async def test_battery_alert_rearms_only_after_soc_clears_the_margin(self) -> None:
        notifier = FakeNotifier()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(battery_soc_percent=34.0),
                    _build_power_snapshot(battery_soc_percent=38.0),
                    _build_power_snapshot(battery_soc_percent=34.0),
                    _build_power_snapshot(battery_soc_percent=41.0),
                    _build_power_snapshot(battery_soc_percent=34.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=FakeStateStore(),
            notifier=notifier,
        )

        await system.control()
        _, partial_recovery_payload = await system.control()
        await system.control()
        _, full_recovery_payload = await system.control()
        await system.control()

        self.assertEqual(
            partial_recovery_payload["next_state"]["battery_alert_latched_percents"], [35.0]
        )
        self.assertEqual(full_recovery_payload["next_state"]["battery_alert_latched_percents"], [])
        self.assertEqual(
            [call["crossed_thresholds"] for call in notifier.battery_alert_calls],
            [(35.0,), (35.0,)],
        )

    async def test_weather_block_alert_is_sent_once_per_weather_day(self) -> None:
        notifier = FakeNotifier()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather(today_sunshine_hours=3.0)),
            state_store=FakeStateStore(),
            notifier=notifier,
            now_provider=_fixed_now(2026, 1, 10, 10, 0),
        )

        await system.control()
        _, payload = await system.control()

        self.assertEqual(len(notifier.weather_block_alert_calls), 1)
        self.assertEqual(notifier.weather_block_alert_calls[0]["local_date"], "2026-01-10")
        self.assertEqual(payload["next_state"]["weather_block_alert_sent_local_date"], "2026-01-10")

    async def test_weather_block_alert_can_send_again_next_weather_day(self) -> None:
        notifier = FakeNotifier()
        state_store = FakeStateStore()
        first_system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather(today_sunshine_hours=3.0)),
            state_store=state_store,
            notifier=notifier,
            now_provider=_fixed_now(2026, 1, 10, 10, 0),
        )
        second_system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather(today_sunshine_hours=3.0)),
            state_store=state_store,
            notifier=notifier,
            now_provider=_fixed_now(2026, 1, 11, 10, 0),
        )

        await first_system.control()
        _, payload = await second_system.control()

        self.assertEqual(len(notifier.weather_block_alert_calls), 2)
        self.assertEqual(
            [call["local_date"] for call in notifier.weather_block_alert_calls],
            ["2026-01-10", "2026-01-11"],
        )
        self.assertEqual(payload["next_state"]["weather_block_alert_sent_local_date"], "2026-01-11")

    async def test_weather_block_alert_triggers_for_unknown_forecast(self) -> None:
        notifier = FakeNotifier()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeAlwaysFailWeatherClient(),
            state_store=FakeStateStore(),
            notifier=notifier,
            now_provider=_fixed_now(2026, 1, 10, 10, 0),
        )

        decision, _ = await system.control()

        self.assertEqual(decision.weather_mode, "unknown")
        self.assertEqual(len(notifier.weather_block_alert_calls), 1)
        self.assertEqual(notifier.weather_block_alert_calls[0]["weather_mode"], "unknown")

    async def test_weather_block_alert_does_not_trigger_for_non_weather_off(self) -> None:
        notifier = FakeNotifier()
        state_store = FakeStateStore()
        low_soc_system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=45.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather(today_sunshine_hours=10.0)),
            state_store=state_store,
            notifier=notifier,
            now_provider=_fixed_now(2026, 1, 10, 10, 0),
        )
        hard_cutoff_system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=28.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather(today_sunshine_hours=10.0)),
            state_store=state_store,
            notifier=notifier,
            now_provider=_fixed_now(2026, 1, 10, 10, 30),
        )

        low_soc_decision, _ = await low_soc_system.control()
        hard_cutoff_decision, _ = await hard_cutoff_system.control()

        self.assertFalse(low_soc_decision.should_turn_on)
        self.assertFalse(hard_cutoff_decision.should_turn_on)
        self.assertEqual(len(notifier.weather_block_alert_calls), 0)

    async def test_manual_shelly_off_during_automatic_on_reasserts_immediately(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=True,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        plug_client = FakePlugClient(status_outputs=[False, True])
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=plug_client,
            state_store=state_store,
        )

        _, payload = await system.control()

        self.assertEqual(payload["actuation"]["status"], "reconciled")
        self.assertEqual(payload["actuation"]["command_sent"], "turn_on")
        self.assertEqual(plug_client.turn_on_calls, 1)
        self.assertEqual(plug_client.turn_off_calls, 0)

    async def test_manual_shelly_on_during_automatic_off_reasserts_immediately(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=False,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=False,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        plug_client = FakePlugClient(status_outputs=[True, False])
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=45.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=plug_client,
            state_store=state_store,
        )

        _, payload = await system.control()

        self.assertEqual(payload["actuation"]["status"], "reconciled")
        self.assertEqual(payload["actuation"]["command_sent"], "turn_off")
        self.assertEqual(plug_client.turn_on_calls, 0)
        self.assertEqual(plug_client.turn_off_calls, 1)

    async def test_control_persists_state_and_cycle_in_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{Path(temp_dir) / 'automation.db'}"
            upgrade_database(database_url)
            with StateStore(database_url) as state_store:
                system = PumpControlSystem(
                    _test_settings(database_url=database_url),
                    probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
                    weather_client=FakeWeatherClient(_build_sunny_weather()),
                    plug_client=FakePlugClient(),
                    state_store=state_store,
                )

                decision, _ = await system.control()

            engine = create_engine_for_url(database_url)
            with engine.begin() as connection:
                state_count = connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM controller_state"
                ).scalar_one()
                cycle_row = connection.exec_driver_sql(
                    "SELECT should_turn_on, actuation_status, actuation_command_sent, weather_source, "
                    "power_status_available, power_status_error "
                    "FROM control_cycle ORDER BY id DESC LIMIT 1"
                ).first()
            engine.dispose()

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(state_count, 1)
        self.assertIsNotNone(cycle_row)
        self.assertEqual(cycle_row[0], 1)
        self.assertEqual(cycle_row[1], "reconciled")
        self.assertEqual(cycle_row[2], "turn_on")
        self.assertEqual(cycle_row[3], "live")
        self.assertEqual(cycle_row[4], 1)
        self.assertIsNone(cycle_row[5])

    async def test_weather_cache_reuses_daily_snapshot_within_process(self) -> None:
        weather_client = FakeCountingWeatherClient([_build_sunny_weather()])
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=weather_client,
            state_store=FakeStateStore(),
        )

        _, payload_one = await system.control()
        _, payload_two = await system.control()

        self.assertEqual(weather_client.fetch_count, 1)
        self.assertEqual(payload_one["weather_source"], "live")
        self.assertEqual(payload_two["weather_source"], "same_day_cache")
        self.assertEqual(payload_one["weather"], payload_two["weather"])

    async def test_weather_fetch_failure_without_cache_returns_unknown_snapshot(self) -> None:
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeAlwaysFailWeatherClient(),
            state_store=FakeStateStore(),
        )

        _, payload = await system.control()

        self.assertEqual(payload["weather_source"], "unavailable")
        self.assertIsNone(payload["weather"]["today_sunshine_hours"])
        self.assertIsNone(payload["weather"]["tomorrow_sunshine_hours"])
        self.assertIsNone(payload["weather"]["current_temperature_c"])
        self.assertEqual(payload["weather"]["queried_timezone"], "Europe/Madrid")

    async def test_same_day_persisted_weather_cache_masks_cross_process_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{Path(temp_dir) / 'automation.db'}"
            upgrade_database(database_url)
            now_provider = _fixed_now(2026, 1, 10, 10, 0)

            with StateStore(database_url) as first_state_store:
                first_system = PumpControlSystem(
                    _test_settings(database_url=database_url),
                    probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
                    weather_client=FakeWeatherClient(_build_sunny_weather()),
                    plug_client=FakePlugClient(status_outputs=[False, True]),
                    state_store=first_state_store,
                    now_provider=now_provider,
                )
                _, first_payload = await first_system.control()

            with StateStore(database_url) as second_state_store:
                second_system = PumpControlSystem(
                    _test_settings(database_url=database_url),
                    probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
                    weather_client=FakeAlwaysFailWeatherClient(),
                    plug_client=FakePlugClient(status_outputs=[True, True]),
                    state_store=second_state_store,
                    now_provider=now_provider,
                )
                second_decision, second_payload = await second_system.control()
                persisted_state = second_state_store.load()

        self.assertEqual(first_payload["weather_source"], "live")
        self.assertEqual(second_payload["weather_source"], "same_day_cache")
        self.assertEqual(second_decision.action, "keep_on")
        self.assertEqual(second_payload["actuation"]["status"], "no_target_change")
        self.assertIsNone(second_payload["actuation"]["command_sent"])
        self.assertEqual(second_payload["weather"], first_payload["weather"])
        self.assertIsNotNone(persisted_state)
        self.assertEqual(persisted_state.weather_cache_local_date, "2026-01-10")

    async def test_previous_day_persisted_weather_cache_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{Path(temp_dir) / 'automation.db'}"
            upgrade_database(database_url)

            with StateStore(database_url) as first_state_store:
                first_system = PumpControlSystem(
                    _test_settings(database_url=database_url),
                    probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
                    weather_client=FakeWeatherClient(_build_sunny_weather()),
                    plug_client=FakePlugClient(status_outputs=[False, True]),
                    state_store=first_state_store,
                    now_provider=_fixed_now(2026, 1, 10, 10, 0),
                )
                await first_system.control()

            with StateStore(database_url) as second_state_store:
                second_system = PumpControlSystem(
                    _test_settings(database_url=database_url),
                    probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
                    weather_client=FakeAlwaysFailWeatherClient(),
                    plug_client=FakePlugClient(status_outputs=[True, False]),
                    state_store=second_state_store,
                    now_provider=_fixed_now(2026, 1, 11, 10, 0),
                )
                second_decision, second_payload = await second_system.control()

        self.assertEqual(second_payload["weather_source"], "unavailable")
        self.assertEqual(second_decision.action, "turn_off")
        self.assertEqual(second_payload["actuation"]["command_sent"], "turn_off")

    async def test_live_weather_refresh_updates_persisted_cache_after_cached_fallback(self) -> None:
        refreshed_weather = _build_sunny_weather(
            current_temperature_c=11.0,
            today_max_temperature_c=20.0,
            today_sunshine_hours=7.0,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_url = f"sqlite:///{Path(temp_dir) / 'automation.db'}"
            upgrade_database(database_url)
            now_provider = _fixed_now(2026, 1, 10, 10, 0)

            with StateStore(database_url) as first_state_store:
                first_system = PumpControlSystem(
                    _test_settings(database_url=database_url),
                    probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
                    weather_client=FakeWeatherClient(_build_sunny_weather()),
                    plug_client=FakePlugClient(status_outputs=[False, True]),
                    state_store=first_state_store,
                    now_provider=now_provider,
                )
                await first_system.control()

            with StateStore(database_url) as second_state_store:
                second_system = PumpControlSystem(
                    _test_settings(database_url=database_url),
                    probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
                    weather_client=FakeAlwaysFailWeatherClient(),
                    plug_client=FakePlugClient(status_outputs=[True, True]),
                    state_store=second_state_store,
                    now_provider=now_provider,
                )
                await second_system.control()

            with StateStore(database_url) as third_state_store:
                third_system = PumpControlSystem(
                    _test_settings(database_url=database_url),
                    probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
                    weather_client=FakeWeatherClient(refreshed_weather),
                    plug_client=FakePlugClient(status_outputs=[True, True]),
                    state_store=third_state_store,
                    now_provider=now_provider,
                )
                _, third_payload = await third_system.control()
                refreshed_state = third_state_store.load()

        self.assertEqual(third_payload["weather_source"], "live")
        self.assertIsNotNone(refreshed_state)
        self.assertEqual(refreshed_state.weather_cache_current_temperature_c, 11.0)
        self.assertEqual(refreshed_state.weather_cache_today_max_temperature_c, 20.0)
        self.assertEqual(refreshed_state.weather_cache_today_sunshine_hours, 7.0)
        self.assertEqual(refreshed_state.weather_cache_tomorrow_sunshine_hours, 10.0)


def _test_settings(**overrides) -> Settings:
    values = {
        "cerbo_host": "cerbo.local",
        "cerbo_port": 502,
        "cerbo_site_name": "Alaro",
        "cerbo_site_identifier": "cerbo-local",
        "cerbo_fetch_retry_count": 2,
        "cerbo_fetch_retry_delay_seconds": 0.0,
        "cerbo_unavailable_grace_cycles": 3,
        "weather_latitude": 39.707337,
        "weather_longitude": 2.791675,
        "weather_timezone": "Europe/Madrid",
        "sunshine_hours_min": 6.5,
        "battery_min_soc_percent": 55.0,
        "battery_soft_min_soc_percent": 35.0,
        "battery_hard_min_soc_percent": 22.5,
        "battery_capacity_kwh": 50.0,
        "auto_off_start_local": "00:00",
        "auto_resume_start_local": "00:00",
        "auto_control_timezone": "UTC",
        "forecast_liberal_sunshine_hours_min": 9.0,
        "forecast_liberal_sunshine_hours_max": 12.0,
        "surplus_night_enabled": True,
        "surplus_night_base_load_kw": 1.5,
        "surplus_night_morning_base_load_kw": 1.75,
        "surplus_night_morning_start_local": "06:00",
        "surplus_night_solar_crossover_local": "08:15",
        "surplus_night_generator_margin_soc_percent": 7.5,
        "surplus_night_pump_load_kw": 2.1,
        "surplus_night_min_run_hours": 1.0,
        "surplus_night_turn_on_margin_soc_percent": 10.0,
        "surplus_night_min_turn_on_margin_soc_percent": 7.0,
        "surplus_night_next_day_sunshine_min": 9.0,
        "state_file": ".state/test-pump-policy-state.json",
        "database_url": "sqlite:///.state/test-automation.db",
        "database_auto_migrate": False,
        "shelly_host": "plug.local",
    }
    values.update(overrides)
    return Settings(**values)


def _build_power_snapshot(
    *,
    battery_soc_percent: float = 82.0,
    solar_watts: float = 3200.0,
    house_watts: float = 900.0,
    generator_watts: float | None = 0.0,
    battery_power_w: float | None = 2300.0,
) -> PowerSnapshot:
    return PowerSnapshot.with_timestamp(
        site_id=1,
        site_name="Alaro",
        site_identifier="cerbo-local",
        battery_soc_percent=battery_soc_percent,
        solar_watts=solar_watts,
        house_watts=house_watts,
        generator_watts=generator_watts,
        active_input_source=240,
        queried_at_unix_ms=1_711_000_000_000,
        house_l1_watts=400.0,
        house_l2_watts=500.0,
        house_l3_watts=None,
        battery_power_w=battery_power_w,
    )


def _build_sunny_weather(
    *,
    current_temperature_c: float = 10.0,
    today_min_temperature_c: float = 8.0,
    today_max_temperature_c: float = 18.0,
    today_sunshine_hours: float = 10.0,
    weather_code: int = 3,
    tomorrow_sunshine_hours: float | None = 10.0,
) -> WeatherSnapshot:
    return WeatherSnapshot(
        current_temperature_c=current_temperature_c,
        today_min_temperature_c=today_min_temperature_c,
        today_max_temperature_c=today_max_temperature_c,
        today_sunshine_hours=today_sunshine_hours,
        weather_code=weather_code,
        queried_timezone="Europe/Madrid",
        tomorrow_sunshine_hours=tomorrow_sunshine_hours,
    )


def _build_switch_status(output: bool) -> ShellySwitchStatus:
    return ShellySwitchStatus(
        switch_id=0,
        output=output,
        source="HTTP_in",
        power_watts=180.0 if output else 0.0,
        voltage_volts=230.0,
        current_amps=0.8 if output else 0.0,
        temperature_c=21.0,
    )


def _fixed_now(year: int, month: int, day: int, hour: int, minute: int):
    timestamp = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return lambda: timestamp


if __name__ == "__main__":
    unittest.main()
