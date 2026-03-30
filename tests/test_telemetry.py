from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

from vrm_solar_automation.config import Settings
from vrm_solar_automation.models import PowerSnapshot
from vrm_solar_automation.runtime import RuntimeSupport
from vrm_solar_automation.system import TelemetryStatus
from vrm_solar_automation.telemetry import (
    CerboMqttClient,
    ControlCoordinator,
    TelemetryHub,
    TelemetryRuntimeStatus,
    _PowerAccumulator,
)
from vrm_solar_automation.weather import WeatherSnapshot


class FakeProbeClient:
    def __init__(self, snapshots: list[PowerSnapshot] | None = None, *, error: Exception | None = None) -> None:
        self._snapshots = list(snapshots or [])
        self._error = error

    async def fetch_snapshot(self) -> PowerSnapshot:
        if self._error is not None and not self._snapshots:
            raise self._error
        if len(self._snapshots) == 1:
            return self._snapshots[0]
        return self._snapshots.pop(0)


class FakeTelemetryHub:
    def __init__(
        self,
        power: PowerSnapshot,
        power_status: TelemetryStatus,
        telemetry: TelemetryRuntimeStatus,
    ) -> None:
        self._power = power
        self._power_status = power_status
        self._telemetry = telemetry
        self._subscribers = []

    def subscribe(self, callback) -> None:
        self._subscribers.append(callback)

    async def get_state(self):
        return self._power, self._power_status, self._telemetry

    async def emit(self) -> None:
        for callback in list(self._subscribers):
            await callback(self._power, self._power_status, self._telemetry)


class FakeWeatherClient:
    async def fetch_weather(self, **kwargs) -> WeatherSnapshot:
        return WeatherSnapshot(
            current_temperature_c=18.0,
            today_min_temperature_c=10.0,
            today_max_temperature_c=26.0,
            weather_code=3,
            queried_timezone="Europe/Madrid",
        )


class FakeCoordinatorSystem:
    def __init__(self) -> None:
        self.control_calls = 0
        self.evaluate_calls = 0

    async def control_with_inputs(self, *, power, weather, power_status):
        self.control_calls += 1
        return None, _build_payload(power, weather, power_status, include_actuation=True)

    async def evaluate_with_inputs(self, *, power, weather, power_status):
        self.evaluate_calls += 1
        return None, _build_payload(power, weather, power_status, include_actuation=False)


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_mqtt_mapping_normalizes_power_snapshot(self) -> None:
        settings = Settings(cerbo_site_identifier="portal-123")
        client = CerboMqttClient(settings)
        accumulator = _PowerAccumulator(settings)

        topics = [
            ("N/portal-123/system/0/Dc/Battery/Soc", b'{"value": 78.5}'),
            ("N/portal-123/system/0/Dc/Pv/Power", b'{"value": 1200}'),
            ("N/portal-123/system/0/Ac/PvOnOutput/L1/Power", b'{"value": 300}'),
            ("N/portal-123/system/0/Ac/Consumption/L1/Power", b'{"value": 400}'),
            ("N/portal-123/system/0/Ac/Consumption/L2/Power", b'{"value": 500}'),
            ("N/portal-123/system/0/Ac/Genset/L1/Power", b'{"value": 50}'),
            ("N/portal-123/system/0/Ac/ActiveIn/Source", b'{"value": 240}'),
        ]

        snapshot = None
        for topic, payload in topics:
            measurement = client.decode_message(topic, payload)
            self.assertIsNotNone(measurement)
            snapshot = accumulator.apply(measurement)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.site_identifier, "portal-123")
        self.assertEqual(snapshot.battery_soc_percent, 78.5)
        self.assertEqual(snapshot.solar_watts, 1500.0)
        self.assertEqual(snapshot.house_watts, 900.0)
        self.assertEqual(snapshot.generator_watts, 50.0)
        self.assertEqual(snapshot.active_input_source, 240)

    async def test_telemetry_hub_deduplicates_equivalent_snapshots(self) -> None:
        settings = Settings()
        hub = TelemetryHub(settings, probe_client=FakeProbeClient())
        notifications: list[PowerSnapshot] = []

        async def subscriber(power, power_status, telemetry):
            del power_status
            del telemetry
            notifications.append(power)

        hub.subscribe(subscriber)
        first = _build_power_snapshot()
        second = PowerSnapshot.with_timestamp(
            site_id=first.site_id,
            site_name=first.site_name,
            site_identifier=first.site_identifier,
            battery_soc_percent=first.battery_soc_percent,
            solar_watts=first.solar_watts,
            house_watts=first.house_watts,
            house_l1_watts=first.house_l1_watts,
            house_l2_watts=first.house_l2_watts,
            house_l3_watts=first.house_l3_watts,
            generator_watts=first.generator_watts,
            active_input_source=first.active_input_source,
            queried_at_unix_ms=first.queried_at_unix_ms + 1000,
        )

        await hub._set_state(
            power=first,
            power_status=TelemetryStatus(source="cerbo_mqtt", available=True, error=None),
            transport="mqtt",
            connected=True,
            fallback_active=False,
            error=None,
            last_message_at=datetime.now(UTC),
        )
        await hub._set_state(
            power=second,
            power_status=TelemetryStatus(source="cerbo_mqtt", available=True, error=None),
            transport="mqtt",
            connected=True,
            fallback_active=False,
            error=None,
            last_message_at=datetime.now(UTC),
        )

        self.assertEqual(len(notifications), 1)

    async def test_telemetry_hub_falls_back_from_unavailable_to_modbus_snapshot(self) -> None:
        settings = Settings()
        snapshot = _build_power_snapshot()
        hub = TelemetryHub(
            settings,
            probe_client=FakeProbeClient(
                [snapshot],
                error=RuntimeError("offline"),
            ),
        )

        hub._probe_client = FakeProbeClient(error=RuntimeError("offline"))
        await hub._poll_modbus_once()
        _, power_status, telemetry = await hub.get_state()
        self.assertFalse(power_status.available)
        self.assertEqual(telemetry.transport, "unavailable")

        hub._probe_client = FakeProbeClient([snapshot])
        await hub._poll_modbus_once()
        power, power_status, telemetry = await hub.get_state()
        self.assertTrue(power_status.available)
        self.assertEqual(telemetry.transport, "modbus_fallback")
        self.assertEqual(power.battery_soc_percent, snapshot.battery_soc_percent)

    async def test_telemetry_hub_fails_fast_for_unsupported_windows_mqtt_runtime(self) -> None:
        settings = Settings(cerbo_mqtt_enabled=True)
        runtime_support = RuntimeSupport(
            platform_system="Windows",
            platform_release="11",
            os_name="nt",
            is_native_windows=True,
            is_wsl=False,
            mqtt_requested=True,
            mqtt_supported=False,
            reason="Native Windows development does not support the Cerbo MQTT transport.",
        )
        hub = TelemetryHub(
            settings,
            probe_client=FakeProbeClient(),
            runtime_support=runtime_support,
        )

        with self.assertRaises(RuntimeError):
            await hub.start()

    async def test_control_coordinator_debounces_rapid_updates(self) -> None:
        settings = Settings(
            cerbo_mock_enabled=True,
            policy_debounce_ms=10,
            policy_min_run_interval_seconds=0.05,
            weather_refresh_seconds=1,
            control_interval_seconds=30.0,
        )
        system = FakeCoordinatorSystem()
        hub = FakeTelemetryHub(
            _build_power_snapshot(),
            TelemetryStatus(source="cerbo_mqtt", available=True, error=None),
            TelemetryRuntimeStatus(
                transport="mqtt",
                connected=True,
                fallback_active=False,
                last_message_at_iso=datetime.now(UTC).isoformat(),
                is_stale=False,
                stale_after_seconds=90.0,
                error=None,
            ),
        )
        coordinator = ControlCoordinator(
            settings,
            hub,
            system_factory=lambda current_settings: system,
            weather_client=FakeWeatherClient(),
        )

        await coordinator.start()
        await hub.emit()
        await hub.emit()
        await hub.emit()
        await asyncio.sleep(0.2)
        await coordinator.stop()

        self.assertEqual(system.control_calls, 2)

    async def test_control_coordinator_skips_automatic_actuation_when_telemetry_is_unavailable(self) -> None:
        settings = Settings(
            cerbo_mock_enabled=True,
            policy_debounce_ms=10,
            policy_min_run_interval_seconds=0.05,
            weather_refresh_seconds=1,
        )
        system = FakeCoordinatorSystem()
        hub = FakeTelemetryHub(
            _build_power_snapshot(),
            TelemetryStatus(source="cerbo_modbus", available=False, error="offline"),
            TelemetryRuntimeStatus(
                transport="unavailable",
                connected=False,
                fallback_active=False,
                last_message_at_iso=None,
                is_stale=True,
                stale_after_seconds=90.0,
                error="offline",
            ),
        )
        coordinator = ControlCoordinator(
            settings,
            hub,
            system_factory=lambda current_settings: system,
            weather_client=FakeWeatherClient(),
        )

        await coordinator.start()
        await coordinator.stop()

        self.assertEqual(system.control_calls, 0)
        self.assertEqual(system.evaluate_calls, 1)


def _build_power_snapshot() -> PowerSnapshot:
    return PowerSnapshot.with_timestamp(
        site_id=1,
        site_name="Alaro",
        site_identifier="cerbo-local",
        battery_soc_percent=82.0,
        solar_watts=3200.0,
        house_watts=900.0,
        house_l1_watts=400.0,
        house_l2_watts=500.0,
        house_l3_watts=None,
        generator_watts=0.0,
        active_input_source=240,
        queried_at_unix_ms=1_711_000_000_000,
    )


def _build_payload(
    power: PowerSnapshot,
    weather: WeatherSnapshot,
    power_status: TelemetryStatus,
    *,
    include_actuation: bool,
) -> dict[str, object]:
    payload = {
        "power": power.to_dict(),
        "power_status": power_status.to_dict(),
        "weather": weather.to_dict(),
        "override": {
            "mode": None,
            "is_active": False,
            "effective_target_is_on": True,
            "reason": None,
            "until_iso": None,
            "seen_auto_off": False,
        },
        "previous_state": None,
        "next_state": {
            "is_on": True,
            "last_known_plug_is_on": True,
            "last_actuation_error": None,
        },
        "decision": {
            "should_turn_on": True,
            "action": "turn_on",
            "reason": "Test decision",
            "reasons": ["Test decision"],
            "weather_mode": "heating",
        },
    }
    if include_actuation:
        payload["actuation"] = {"status": "reconciled"}
    return payload


if __name__ == "__main__":
    unittest.main()
