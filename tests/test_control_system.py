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
from vrm_solar_automation.shelly import ShellySwitchCommandResult, ShellySwitchStatus
from vrm_solar_automation.state import StateStore
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
        self.cycles: list[dict[str, object]] = []

    def load(self):
        return self.state

    def save(self, state) -> None:
        self.state = state

    def record_control_cycle(self, **kwargs) -> None:
        self.cycles.append(kwargs)


class FakeNotifier:
    def __init__(self, *, should_raise: bool = False) -> None:
        self.should_raise = should_raise
        self.calls: list[dict[str, object]] = []
        self.battery_alert_calls: list[dict[str, object]] = []
        self.generator_alert_calls: list[dict[str, object]] = []

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


class PumpPolicyAndControlTests(unittest.IsolatedAsyncioTestCase):
    def test_generator_power_blocks_operation(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=1200.0),
            weather=_build_sunny_weather(),
            previous_state=None,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_off")
        self.assertIn("Generator power is present", decision.reason)

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
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.weather_mode, "unknown")
        self.assertIn("sunshine-hours forecast is unavailable", decision.reason)

    def test_insufficient_sun_keeps_operation_off(self) -> None:
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0),
            weather=_build_sunny_weather(today_sunshine_hours=3.5),
            previous_state=None,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.weather_mode, "insufficient_sun")
        self.assertIn("below the 4.5-hour minimum", decision.reason)

    def test_soc_at_minimum_keeps_operation_off(self) -> None:
        previous_state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        decision = PumpPolicy().decide(
            power=_build_power_snapshot(generator_watts=0.0, battery_soc_percent=45.0),
            weather=_build_sunny_weather(),
            previous_state=previous_state,
            now=datetime(2026, 1, 10, tzinfo=UTC),
        )

        self.assertFalse(decision.should_turn_on)
        self.assertEqual(decision.action, "turn_off")
        self.assertIn("at or below the 45.0% minimum", decision.reason)

    async def test_control_uses_configured_soc_threshold(self) -> None:
        system = PumpControlSystem(
            _test_settings(
                battery_min_soc_percent=45.0,
            ),
            probe_client=FakeProbeClient(
                _build_power_snapshot(generator_watts=0.0, battery_soc_percent=46.0)
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=FakeStateStore(),
        )

        decision, payload = await system.evaluate()

        self.assertTrue(decision.should_turn_on)
        self.assertTrue(payload["next_state"]["is_on"])
        self.assertIn("46.0%, above the 45.0% minimum run threshold", decision.reason)

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
        self.assertIn("Cerbo GX", payload["power_status"]["error"])
        self.assertIsNone(payload["power"]["battery_soc_percent"])
        self.assertFalse(decision.should_turn_on)
        self.assertIn("Battery SOC is unavailable", payload["decision"]["reason"])

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

    async def test_no_target_change_does_not_send_email(self) -> None:
        notifier = FakeNotifier()
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=False,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(_build_power_snapshot(generator_watts=0.0)),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=FakePlugClient(status_outputs=[False, False]),
            state_store=state_store,
            notifier=notifier,
        )

        _, payload = await system.control()

        self.assertEqual(payload["actuation"]["status"], "no_target_change")
        self.assertIsNone(payload["actuation"]["command_sent"])
        self.assertEqual(notifier.calls, [])

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

    async def test_battery_alerts_latch_until_soc_recovers(self) -> None:
        notifier = FakeNotifier()
        state_store = FakeStateStore()
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(battery_soc_percent=39.0),
                    _build_power_snapshot(battery_soc_percent=34.0),
                    _build_power_snapshot(battery_soc_percent=29.0),
                    _build_power_snapshot(battery_soc_percent=29.0),
                    _build_power_snapshot(battery_soc_percent=41.0),
                    _build_power_snapshot(battery_soc_percent=39.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            state_store=state_store,
            notifier=notifier,
        )

        await system.control()
        await system.control()
        await system.control()
        await system.control()
        recovery_decision, recovery_payload = await system.control()
        _, final_payload = await system.control()

        self.assertFalse(recovery_decision.should_turn_on)
        self.assertEqual(
            [call["crossed_thresholds"] for call in notifier.battery_alert_calls],
            [(40,), (35,), (30,), (40,)],
        )
        self.assertFalse(recovery_payload["next_state"]["battery_alert_below_40_sent"])
        self.assertFalse(recovery_payload["next_state"]["battery_alert_below_35_sent"])
        self.assertFalse(recovery_payload["next_state"]["battery_alert_below_30_sent"])
        self.assertTrue(final_payload["next_state"]["battery_alert_below_40_sent"])
        self.assertFalse(final_payload["next_state"]["battery_alert_below_35_sent"])
        self.assertFalse(final_payload["next_state"]["battery_alert_below_30_sent"])

    async def test_manual_shelly_off_during_automatic_on_waits_for_next_off_then_on(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=True,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=True,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        plug_client = FakePlugClient(status_outputs=[False, False, False, False, True])
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=45.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=plug_client,
            state_store=state_store,
        )

        _, first_payload = await system.control()
        _, second_payload = await system.control()
        _, third_payload = await system.control()

        self.assertEqual(first_payload["actuation"]["status"], "no_target_change")
        self.assertIsNone(first_payload["actuation"]["command_sent"])
        self.assertEqual(second_payload["actuation"]["status"], "already_aligned")
        self.assertIsNone(second_payload["actuation"]["command_sent"])
        self.assertEqual(third_payload["actuation"]["status"], "reconciled")
        self.assertEqual(third_payload["actuation"]["command_sent"], "turn_on")
        self.assertEqual(plug_client.turn_on_calls, 1)
        self.assertEqual(plug_client.turn_off_calls, 0)

    async def test_manual_shelly_on_during_automatic_off_waits_for_next_on_then_off(self) -> None:
        state_store = FakeStateStore()
        state_store.state = PumpPolicyState(
            is_on=False,
            changed_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
            last_known_plug_is_on=False,
            last_known_plug_at_iso=datetime(2026, 1, 10, tzinfo=UTC).isoformat(),
        )
        plug_client = FakePlugClient(status_outputs=[True, True, True, True, False])
        system = PumpControlSystem(
            _test_settings(),
            probe_client=FakeProbeClient(
                [
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=45.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=82.0),
                    _build_power_snapshot(generator_watts=0.0, battery_soc_percent=45.0),
                ]
            ),
            weather_client=FakeWeatherClient(_build_sunny_weather()),
            plug_client=plug_client,
            state_store=state_store,
        )

        _, first_payload = await system.control()
        _, second_payload = await system.control()
        _, third_payload = await system.control()

        self.assertEqual(first_payload["actuation"]["status"], "no_target_change")
        self.assertIsNone(first_payload["actuation"]["command_sent"])
        self.assertEqual(second_payload["actuation"]["status"], "already_aligned")
        self.assertIsNone(second_payload["actuation"]["command_sent"])
        self.assertEqual(third_payload["actuation"]["status"], "reconciled")
        self.assertEqual(third_payload["actuation"]["command_sent"], "turn_off")
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
                    "SELECT should_turn_on, actuation_status, actuation_command_sent, weather_source "
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


def _test_settings(**overrides) -> Settings:
    values = {
        "cerbo_host": "cerbo.local",
        "cerbo_port": 502,
        "cerbo_site_name": "Alaro",
        "cerbo_site_identifier": "cerbo-local",
        "weather_latitude": 39.707337,
        "weather_longitude": 2.791675,
        "weather_timezone": "Europe/Madrid",
        "sunshine_hours_min": 4.5,
        "battery_min_soc_percent": 45.0,
        "auto_off_start_local": "00:00",
        "auto_resume_start_local": "00:00",
        "auto_control_timezone": "UTC",
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
    )


def _build_sunny_weather(
    *,
    current_temperature_c: float = 10.0,
    today_min_temperature_c: float = 8.0,
    today_max_temperature_c: float = 18.0,
    today_sunshine_hours: float = 6.0,
    weather_code: int = 3,
) -> WeatherSnapshot:
    return WeatherSnapshot(
        current_temperature_c=current_temperature_c,
        today_min_temperature_c=today_min_temperature_c,
        today_max_temperature_c=today_max_temperature_c,
        today_sunshine_hours=today_sunshine_hours,
        weather_code=weather_code,
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
        temperature_c=21.0,
    )


def _fixed_now(year: int, month: int, day: int, hour: int, minute: int):
    timestamp = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return lambda: timestamp


if __name__ == "__main__":
    unittest.main()
