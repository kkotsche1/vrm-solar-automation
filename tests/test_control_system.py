from __future__ import annotations

import unittest
from datetime import UTC, datetime

from vrm_solar_automation.config import Settings
from vrm_solar_automation.models import PowerSnapshot
from vrm_solar_automation.policy import PumpPolicy, PumpPolicyState
from vrm_solar_automation.shelly import ShellySwitchCommandResult, ShellySwitchStatus
from vrm_solar_automation.system import PumpControlSystem
from vrm_solar_automation.weather import WeatherSnapshot


class FakeProbeClient:
    def __init__(self, snapshot: PowerSnapshot | list[PowerSnapshot]) -> None:
        if isinstance(snapshot, list):
            self._snapshots = list(snapshot)
        else:
            self._snapshots = [snapshot]

    async def fetch_snapshot(self) -> PowerSnapshot:
        if len(self._snapshots) == 1:
            return self._snapshots[0]
        return self._snapshots.pop(0)


class FakeWeatherClient:
    def __init__(self, snapshot: WeatherSnapshot) -> None:
        self._snapshot = snapshot

    async def fetch_weather(self, **kwargs) -> WeatherSnapshot:
        return self._snapshot


class FakeUnavailableProbeClient:
    async def fetch_snapshot(self) -> PowerSnapshot:
        raise TimeoutError("timed out")


class FakePlugClient:
    def __init__(self, status_outputs: list[bool] | None = None) -> None:
        raw_outputs = status_outputs or [False, True]
        self._status_reads = [_build_switch_status(output) for output in raw_outputs]
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
            output=True,
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
            output=False,
            source="HTTP_in",
            toggle_after_seconds=None,
            executed_at_iso=datetime.now(UTC).isoformat(),
        )


class FakeStateStore:
    def __init__(self) -> None:
        self.state = None

    def load(self):
        return self.state

    def save(self, state) -> None:
        self.state = state


class PumpPolicyAndControlTests(unittest.IsolatedAsyncioTestCase):
    def test_generator_power_blocks_operation(self) -> None:
        policy = PumpPolicy()
        decision = policy.decide(
            power=_build_power_snapshot(generator_watts=1200.0),
            weather=_build_heating_weather(),
            previous_state=None,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_off")
        self.assertIn("Generator power is present", decision.reason)

    def test_weather_unknown_keeps_operation_off(self) -> None:
        policy = PumpPolicy()
        decision = policy.decide(
            power=_build_power_snapshot(generator_watts=0.0),
            weather=WeatherSnapshot(
                current_temperature_c=None,
                today_min_temperature_c=None,
                today_max_temperature_c=None,
                weather_code=None,
                queried_timezone="Europe/Madrid",
            ),
            previous_state=None,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.weather_mode, "unknown")
        self.assertIn("automatic control stays off", decision.reason)

    def test_battery_soc_at_or_below_reserve_turns_off(self) -> None:
        policy = PumpPolicy()
        decision = policy.decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=50.0),
            weather=_build_heating_weather(),
            previous_state=None,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_off")
        self.assertIn("50.0% reserve", decision.reason)

    def test_battery_soc_above_run_threshold_turns_on_with_demand(self) -> None:
        policy = PumpPolicy()
        decision = policy.decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0),
            weather=_build_heating_weather(),
            previous_state=None,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_on")
        self.assertIn("60.0% run threshold", decision.reason)

    def test_hysteresis_band_keeps_previous_on_state(self) -> None:
        policy = PumpPolicy()
        previous_state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        decision = policy.decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=55.0),
            weather=_build_heating_weather(),
            previous_state=previous_state,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(decision.action, "keep_on")
        self.assertIn("previous automatic state stays on", decision.reason)

    def test_hysteresis_band_keeps_previous_off_state(self) -> None:
        policy = PumpPolicy()
        previous_state = PumpPolicyState(
            is_on=False,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        decision = policy.decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=55.0),
            weather=_build_heating_weather(),
            previous_state=previous_state,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "keep_off")
        self.assertIn("previous automatic state stays off", decision.reason)

    def test_hysteresis_band_defaults_to_off_without_previous_state(self) -> None:
        policy = PumpPolicy()
        decision = policy.decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=55.0),
            weather=_build_heating_weather(),
            previous_state=None,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_off")
        self.assertIn("no previous automatic state", decision.reason)

    def test_weather_classifies_heating_from_daily_low(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0),
            weather=_build_weather(today_min_temperature_c=12.0, today_max_temperature_c=20.0),
            previous_state=None,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertEqual(decision.weather_mode, "heating")
        self.assertTrue(decision.should_turn_on)

    def test_weather_classifies_cooling_from_daily_high(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0),
            weather=_build_weather(today_min_temperature_c=18.0, today_max_temperature_c=26.0),
            previous_state=None,
            now=datetime(2026, 7, 10, tzinfo=UTC),
        )

        self.assertEqual(decision.weather_mode, "cooling")
        self.assertTrue(decision.should_turn_on)

    def test_weather_classifies_mixed_when_forecast_spans_both_edges(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0),
            weather=_build_weather(today_min_temperature_c=10.0, today_max_temperature_c=28.0),
            previous_state=None,
            now=datetime(2026, 4, 10, tzinfo=UTC),
        )

        self.assertEqual(decision.weather_mode, "mixed")
        self.assertTrue(decision.should_turn_on)

    def test_weather_classifies_mild_inside_comfort_band(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=60.0),
            weather=_build_weather(today_min_temperature_c=14.0, today_max_temperature_c=24.0),
            previous_state=None,
            now=datetime(2026, 4, 10, tzinfo=UTC),
        )

        self.assertEqual(decision.weather_mode, "mild")
        self.assertFalse(decision.should_turn_on)

    async def test_control_reconciles_shelly_to_intended_state(self) -> None:
        state_store = FakeStateStore()
        system = PumpControlSystem(
            Settings(state_file=".state/test-state.json"),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_heating_weather()),
            plug_client=FakePlugClient(),
            state_store=state_store,
        )

        decision, payload = await system.control()

        self.assertTrue(decision.should_turn_on)
        self.assertEqual(payload["actuation"]["status"], "reconciled")
        self.assertEqual(payload["actuation"]["command_sent"], "turn_on")
        self.assertTrue(payload["next_state"]["last_known_plug_is_on"])

    async def test_evaluate_is_read_only_for_dashboard_polling(self) -> None:
        state_store = FakeStateStore()
        previous_state = PumpPolicyState(
            is_on=False,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        state_store.state = previous_state
        system = PumpControlSystem(
            Settings(state_file=".state/test-state.json"),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_heating_weather()),
            state_store=state_store,
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["next_state"]["is_on"])
        self.assertIs(state_store.state, previous_state)

    async def test_manual_on_override_forces_on_temporarily(self) -> None:
        state_store = FakeStateStore()
        system = PumpControlSystem(
            Settings(state_file=".state/test-state.json"),
            plug_client=FakePlugClient(status_outputs=[False, True]),
            state_store=state_store,
        )

        payload = await system.set_manual_on_override(duration_minutes=30)

        self.assertEqual(payload["override"]["mode"], "manual_on_until")
        self.assertTrue(payload["override"]["is_active"])
        self.assertTrue(payload["override"]["effective_target_is_on"])
        self.assertEqual(payload["actuation"]["command_sent"], "turn_on")

    async def test_evaluate_gracefully_degrades_when_cerbo_is_unavailable(self) -> None:
        system = PumpControlSystem(
            Settings(state_file=".state/test-state.json"),
            probe_client=FakeUnavailableProbeClient(),
            weather_client=FakeWeatherClient(_build_heating_weather()),
            state_store=FakeStateStore(),
        )

        decision, payload = await system.evaluate()

        self.assertFalse(payload["power_status"]["available"])
        self.assertIn("Cerbo GX", payload["power_status"]["error"])
        self.assertIsNone(payload["power"]["battery_soc_percent"])
        self.assertFalse(decision.should_turn_on)
        self.assertIn("Battery SOC is unavailable", payload["decision"]["reason"])

    async def test_evaluate_uses_mock_cerbo_snapshot_when_enabled(self) -> None:
        system = PumpControlSystem(
            Settings(
                state_file=".state/test-state.json",
                cerbo_mock_enabled=True,
                cerbo_site_name="Mock Cerbo GX",
                cerbo_site_identifier="cerbo-mock",
            ),
            weather_client=FakeWeatherClient(_build_heating_weather()),
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

    async def test_manual_off_until_next_auto_on_waits_for_fresh_cycle(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            override_mode="manual_off_until_next_auto_on",
            override_set_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            override_seen_auto_off=False,
        )
        system = PumpControlSystem(
            Settings(state_file=".state/test-state.json"),
            policy=PumpPolicy(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=50.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_heating_weather()),
            plug_client=FakePlugClient(status_outputs=[True, False, False, True]),
            state_store=state_store,
        )

        first_decision, first_payload = await system.control()
        second_decision, second_payload = await system.control()

        self.assertFalse(first_decision.should_turn_on)
        self.assertTrue(first_payload["override"]["is_active"])
        self.assertTrue(first_payload["override"]["seen_auto_off"])
        self.assertFalse(first_payload["override"]["effective_target_is_on"])
        self.assertTrue(second_decision.should_turn_on)
        self.assertFalse(second_payload["override"]["is_active"])
        self.assertTrue(second_payload["override"]["effective_target_is_on"])
        self.assertEqual(second_payload["actuation"]["command_sent"], "turn_on")

    async def test_emergency_off_persists_until_cleared(self) -> None:
        state_store = FakeStateStore()
        system = PumpControlSystem(
            Settings(state_file=".state/test-state.json"),
            policy=PumpPolicy(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0)),
            weather_client=FakeWeatherClient(_build_heating_weather()),
            plug_client=FakePlugClient(status_outputs=[True, False, False, False, False]),
            state_store=state_store,
        )

        emergency_payload = await system.set_emergency_off_override()
        _, evaluate_payload = await system.evaluate()

        self.assertEqual(emergency_payload["override"]["mode"], "emergency_off")
        self.assertTrue(emergency_payload["override"]["is_active"])
        self.assertFalse(emergency_payload["override"]["effective_target_is_on"])
        self.assertTrue(evaluate_payload["override"]["is_active"])
        self.assertEqual(evaluate_payload["override"]["mode"], "emergency_off")
        self.assertFalse(evaluate_payload["override"]["effective_target_is_on"])


def _build_power_snapshot(
    *,
    generator_watts: float | None,
    battery_soc_percent: float = 82.0,
) -> PowerSnapshot:
    return PowerSnapshot.with_timestamp(
        site_id=1,
        site_name="Alaro",
        site_identifier="cerbo-local",
        battery_soc_percent=battery_soc_percent,
        solar_watts=3200.0,
        house_watts=900.0,
        generator_watts=generator_watts,
        active_input_source=None,
        queried_at_unix_ms=1_711_000_000_000,
    )


def _build_heating_weather() -> WeatherSnapshot:
    return _build_weather(
        current_temperature_c=9.0,
        today_min_temperature_c=7.0,
        today_max_temperature_c=15.0,
    )


def _build_weather(
    *,
    current_temperature_c: float | None = 9.0,
    today_min_temperature_c: float | None,
    today_max_temperature_c: float | None,
) -> WeatherSnapshot:
    return WeatherSnapshot(
        current_temperature_c=current_temperature_c,
        today_min_temperature_c=today_min_temperature_c,
        today_max_temperature_c=today_max_temperature_c,
        weather_code=3,
        queried_timezone="Europe/Madrid",
    )


def _build_switch_status(output: bool) -> ShellySwitchStatus:
    return ShellySwitchStatus(
        switch_id=0,
        output=output,
        source="HTTP_in",
        power_watts=180.0 if output else 0.0,
        voltage_volts=230.0,
        current_amps=0.8 if output else 0.0,
        temperature_c=22.0,
    )


if __name__ == "__main__":
    unittest.main()
