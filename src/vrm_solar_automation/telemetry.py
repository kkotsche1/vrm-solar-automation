from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp

from . import db
from .client import ProbeUnavailableError, VrmProbeClient
from .config import Settings
from .mock_data import build_mock_power_snapshot
from .models import PowerSnapshot
from .runtime import RuntimeSupport, detect_runtime_support, ensure_supported_runtime
from .shelly import ShellyError, ShellyPlugClient
from .system import PumpControlSystem, TelemetryStatus
from .weather import OpenMeteoClient, WeatherSnapshot

try:
    import aiomqtt
except ImportError:  # pragma: no cover - exercised only when dependency is absent
    aiomqtt = None

logger = logging.getLogger(__name__)

TelemetrySubscriber = Callable[[PowerSnapshot, TelemetryStatus, "TelemetryRuntimeStatus"], Awaitable[None] | None]
StatusSubscriber = Callable[[dict[str, object]], Awaitable[None] | None]
SystemFactory = Callable[[Settings], PumpControlSystem]
PlugClientFactory = Callable[[Settings], ShellyPlugClient]

DEFAULT_WEATHER = WeatherSnapshot(
    current_temperature_c=None,
    today_min_temperature_c=None,
    today_max_temperature_c=None,
    weather_code=None,
    queried_timezone="UTC",
)


@dataclass(frozen=True)
class TelemetryRuntimeStatus:
    transport: str
    connected: bool
    fallback_active: bool
    last_message_at_iso: str | None
    is_stale: bool
    stale_after_seconds: float
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "transport": self.transport,
            "connected": self.connected,
            "fallback_active": self.fallback_active,
            "last_message_at_iso": self.last_message_at_iso,
            "is_stale": self.is_stale,
            "stale_after_seconds": self.stale_after_seconds,
            "error": self.error,
        }


@dataclass(frozen=True)
class MqttMeasurement:
    portal_id: str
    path: tuple[str, ...]
    value: object | None
    received_at: datetime


@dataclass
class _PowerAccumulator:
    settings: Settings
    portal_id: str | None = None
    values: dict[str, float | int | None] = field(default_factory=dict)

    def apply(self, measurement: MqttMeasurement) -> PowerSnapshot | None:
        key = self._map_path(measurement.path)
        if key is None:
            return None

        self.portal_id = measurement.portal_id or self.portal_id
        if key == "active_input_source":
            self.values[key] = _to_int(measurement.value)
        else:
            self.values[key] = _to_float(measurement.value)
        return self.snapshot(measurement.received_at)

    def snapshot(self, received_at: datetime | None = None) -> PowerSnapshot:
        timestamp = received_at or datetime.now(UTC)
        queried_at_unix_ms = int(timestamp.timestamp() * 1000)
        return PowerSnapshot.with_timestamp(
            site_id=self.settings.site_id or 0,
            site_name=self.settings.cerbo_site_name,
            site_identifier=self.portal_id or self.settings.cerbo_site_identifier,
            battery_soc_percent=_to_float(self.values.get("battery_soc")),
            solar_watts=self._solar_watts(),
            house_watts=self._house_watts(),
            house_l1_watts=_to_float(self.values.get("house_l1")),
            house_l2_watts=_to_float(self.values.get("house_l2")),
            house_l3_watts=_to_float(self.values.get("house_l3")),
            generator_watts=self._generator_watts(),
            active_input_source=_to_int(self.values.get("active_input_source")),
            queried_at_unix_ms=queried_at_unix_ms,
        )

    def _solar_watts(self) -> float | None:
        solar_total = _to_float(self.values.get("pv_dc"))
        for prefix in ("pv_output", "pv_grid", "pv_genset"):
            phase_total = self._phase_group_total(prefix)
            if phase_total is None:
                continue
            solar_total = (solar_total or 0.0) + phase_total
        return solar_total

    def _house_watts(self) -> float | None:
        phases = [_to_float(self.values.get("house_l1")), _to_float(self.values.get("house_l2")), _to_float(self.values.get("house_l3"))]
        if any(value is not None for value in phases):
            return float(sum(value for value in phases if value is not None))
        return _to_float(self.values.get("house_total"))

    def _generator_watts(self) -> float | None:
        return self._phase_group_total("generator")

    def _phase_group_total(self, prefix: str) -> float | None:
        total = _to_float(self.values.get(f"{prefix}_total"))
        if total is not None:
            return total
        phases = [
            _to_float(self.values.get(f"{prefix}_l1")),
            _to_float(self.values.get(f"{prefix}_l2")),
            _to_float(self.values.get(f"{prefix}_l3")),
        ]
        if any(value is not None for value in phases):
            return float(sum(value for value in phases if value is not None))
        return None

    @staticmethod
    def _map_path(path: tuple[str, ...]) -> str | None:
        if path == ("Dc", "Battery", "Soc"):
            return "battery_soc"
        if path == ("Dc", "Pv", "Power"):
            return "pv_dc"
        if path in {("Ac", "ActiveIn", "Source"), ("Ac", "ActiveInput", "Source")}:
            return "active_input_source"
        if path == ("Ac", "Consumption", "Total", "Power"):
            return "house_total"

        if len(path) != 4 or path[0] != "Ac" or path[-1] != "Power":
            return None

        group = path[1]
        suffix = path[2].lower()
        if group in {"Consumption", "ConsumptionOnOutput"} and suffix in {"l1", "l2", "l3"}:
            return f"house_{suffix}"
        if group in {"PvOnOutput", "PvOnGrid", "PvOnGenset"} and suffix in {"l1", "l2", "l3", "total"}:
            group_key = {
                "PvOnOutput": "pv_output",
                "PvOnGrid": "pv_grid",
                "PvOnGenset": "pv_genset",
            }[group]
            return f"{group_key}_{suffix}"
        if group in {"Genset", "Generator"} and suffix in {"l1", "l2", "l3", "total"}:
            return f"generator_{suffix}"
        return None


class CerboMqttClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        identifier = settings.cerbo_site_identifier.strip()
        self.portal_id_hint = identifier if identifier and identifier != "cerbo-local" else None

    def build_client(self):
        if aiomqtt is None:
            raise RuntimeError("aiomqtt is not installed. Install dependencies to enable CERBO_MQTT_ENABLED.")
        return aiomqtt.Client(
            hostname=self._settings.cerbo_mqtt_host or self._settings.cerbo_host,
            port=self._settings.cerbo_mqtt_port,
            username=self._settings.cerbo_mqtt_username,
            password=self._settings.cerbo_mqtt_password,
        )

    async def subscribe(self, client: Any) -> None:
        await client.subscribe("N/+/system/0/#")
        await client.subscribe("N/+/full_publish_completed")

    async def send_keepalive(
        self,
        client: Any,
        portal_id: str,
        *,
        suppress_republish: bool,
    ) -> None:
        payload = ""
        if suppress_republish:
            payload = json.dumps({"keepalive-options": ["suppress-republish"]})
        await client.publish(f"R/{portal_id}/keepalive", payload=payload)

    def decode_message(self, topic: str, payload: bytes | str | None) -> MqttMeasurement | None:
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != "N":
            return None
        portal_id = parts[1]
        if len(parts) == 3 and parts[2] == "full_publish_completed":
            return None
        if len(parts) < 5 or parts[2] != "system" or parts[3] != "0":
            return None

        raw_value = None
        if payload not in (None, b"", ""):
            text = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                raw_value = parsed.get("value")

        return MqttMeasurement(
            portal_id=portal_id,
            path=tuple(parts[4:]),
            value=raw_value,
            received_at=datetime.now(UTC),
        )


class TelemetryHub:
    def __init__(
        self,
        settings: Settings,
        *,
        probe_client: VrmProbeClient | None = None,
        mqtt_client: CerboMqttClient | None = None,
        runtime_support: RuntimeSupport | None = None,
    ) -> None:
        self._settings = settings
        self._probe_client = probe_client or VrmProbeClient(settings)
        self._mqtt_client = mqtt_client or CerboMqttClient(settings)
        self._runtime_support = runtime_support or detect_runtime_support(settings)
        self._subscribers: set[TelemetrySubscriber] = set()
        self._lock = asyncio.Lock()
        self._power = _build_unavailable_snapshot(settings)
        self._power_status = TelemetryStatus(
            source=self._initial_source(),
            available=False,
            error="Telemetry has not produced a snapshot yet.",
        )
        self._transport = "unavailable"
        self._connected = False
        self._fallback_active = False
        self._error: str | None = "Telemetry has not produced a snapshot yet."
        self._last_message_at: datetime | None = None
        self._mqtt_connected = False
        self._mqtt_task: asyncio.Task[None] | None = None
        self._fallback_task: asyncio.Task[None] | None = None
        self._accumulator = _PowerAccumulator(settings)
        self._keepalive_sent_portals: set[str] = set()

    def subscribe(self, callback: TelemetrySubscriber) -> None:
        self._subscribers.add(callback)

    async def start(self) -> None:
        ensure_supported_runtime(self._settings, self._runtime_support)
        if self._settings.cerbo_mock_enabled:
            snapshot = build_mock_power_snapshot(self._settings)
            await self._set_state(
                power=snapshot,
                power_status=TelemetryStatus(source="cerbo_mock", available=True, error=None),
                transport="mock",
                connected=True,
                fallback_active=False,
                error=None,
                last_message_at=datetime.now(UTC),
            )
            return

        await self._poll_modbus_once()
        self._fallback_task = asyncio.create_task(self._run_fallback_loop())
        if self._settings.cerbo_mqtt_enabled:
            self._mqtt_task = asyncio.create_task(self._run_mqtt_loop())

    async def stop(self) -> None:
        tasks = [task for task in (self._mqtt_task, self._fallback_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def get_state(self) -> tuple[PowerSnapshot, TelemetryStatus, TelemetryRuntimeStatus]:
        async with self._lock:
            return self._power, self._power_status, self._runtime_snapshot_locked()

    async def _run_fallback_loop(self) -> None:
        interval = max(1.0, float(self._settings.modbus_fallback_poll_seconds))
        while True:
            await asyncio.sleep(interval)
            if await self._should_poll_modbus():
                await self._poll_modbus_once()

    async def _should_poll_modbus(self) -> bool:
        async with self._lock:
            if self._settings.cerbo_mock_enabled:
                return False
            if not self._settings.cerbo_mqtt_enabled:
                return True
            if not self._mqtt_connected:
                return True
            return self._is_stale_locked()

    async def _poll_modbus_once(self) -> None:
        try:
            snapshot = await self._probe_client.fetch_snapshot()
        except (ProbeUnavailableError, OSError, TimeoutError, RuntimeError) as exc:
            async with self._lock:
                transport = self._transport if self._transport != "unavailable" else "unavailable"
                error = str(exc)
                previous_signature = self._notification_signature_locked()
                self._power_status = TelemetryStatus(
                    source="cerbo_modbus",
                    available=False,
                    error=error,
                )
                if self._transport != "mqtt":
                    self._transport = "unavailable"
                    self._connected = False
                    self._fallback_active = False
                    self._error = error
                runtime = self._runtime_snapshot_locked()
                current_signature = self._notification_signature_locked()
                power = self._power
                power_status = self._power_status
                notify = previous_signature != current_signature and transport != "mqtt"
            if notify:
                await self._notify_subscribers(power, power_status, runtime)
            return

        await self._set_state(
            power=snapshot,
            power_status=TelemetryStatus(source="cerbo_modbus", available=True, error=None),
            transport="modbus_fallback",
            connected=True,
            fallback_active=True,
            error=None,
            last_message_at=datetime.now(UTC),
        )

    async def _run_mqtt_loop(self) -> None:
        reconnect_delay_seconds = 5.0
        while True:
            try:
                client = self._mqtt_client.build_client()
                async with client:
                    await self._mqtt_client.subscribe(client)
                    await self._set_mqtt_connectivity(connected=True, error=None)
                    if self._mqtt_client.portal_id_hint:
                        await self._send_keepalive_once(client, self._mqtt_client.portal_id_hint, suppress_republish=False)
                    async for message in client.messages:
                        payload = _coerce_payload_bytes(message.payload)
                        topic = str(message.topic)
                        measurement = self._mqtt_client.decode_message(topic, payload)
                        if measurement is None:
                            continue
                        if measurement.portal_id:
                            await self._send_keepalive_once(client, measurement.portal_id, suppress_republish=False)
                        snapshot = self._accumulator.apply(measurement)
                        if snapshot is None:
                            continue
                        await self._set_state(
                            power=snapshot,
                            power_status=TelemetryStatus(source="cerbo_mqtt", available=True, error=None),
                            transport="mqtt",
                            connected=True,
                            fallback_active=False,
                            error=None,
                            last_message_at=measurement.received_at,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._set_mqtt_connectivity(connected=False, error=str(exc))
                logger.warning("Cerbo MQTT loop disconnected: %s", exc)
                await asyncio.sleep(reconnect_delay_seconds)

    async def _send_keepalive_once(self, client: Any, portal_id: str, *, suppress_republish: bool) -> None:
        if portal_id in self._keepalive_sent_portals:
            if suppress_republish:
                await self._mqtt_client.send_keepalive(client, portal_id, suppress_republish=True)
            return
        await self._mqtt_client.send_keepalive(client, portal_id, suppress_republish=suppress_republish)
        self._keepalive_sent_portals.add(portal_id)

    async def _set_mqtt_connectivity(self, *, connected: bool, error: str | None) -> None:
        async with self._lock:
            previous_signature = self._notification_signature_locked()
            self._mqtt_connected = connected
            if connected:
                if self._transport == "mqtt":
                    self._connected = True
                    self._error = None
            elif self._transport == "mqtt":
                self._connected = False
                self._error = error
            runtime = self._runtime_snapshot_locked()
            current_signature = self._notification_signature_locked()
            power = self._power
            power_status = self._power_status
            notify = previous_signature != current_signature
        if notify:
            await self._notify_subscribers(power, power_status, runtime)

    async def _set_state(
        self,
        *,
        power: PowerSnapshot,
        power_status: TelemetryStatus,
        transport: str,
        connected: bool,
        fallback_active: bool,
        error: str | None,
        last_message_at: datetime,
    ) -> None:
        async with self._lock:
            previous_signature = self._notification_signature_locked()
            self._power = power
            self._power_status = power_status
            self._transport = transport
            self._connected = connected
            self._fallback_active = fallback_active
            self._error = error
            self._last_message_at = last_message_at
            runtime = self._runtime_snapshot_locked()
            current_signature = self._notification_signature_locked()
            payload_power = self._power
            payload_status = self._power_status
            notify = previous_signature != current_signature
        if notify:
            await self._notify_subscribers(payload_power, payload_status, runtime)

    async def _notify_subscribers(
        self,
        power: PowerSnapshot,
        power_status: TelemetryStatus,
        runtime: TelemetryRuntimeStatus,
    ) -> None:
        for callback in list(self._subscribers):
            await _invoke_callback(callback, power, power_status, runtime)

    def _initial_source(self) -> str:
        if self._settings.cerbo_mock_enabled:
            return "cerbo_mock"
        if self._settings.cerbo_mqtt_enabled:
            return "cerbo_mqtt"
        return "cerbo_modbus"

    def _is_stale_locked(self) -> bool:
        if self._last_message_at is None:
            return True
        age_seconds = (datetime.now(UTC) - self._last_message_at).total_seconds()
        return age_seconds > float(self._settings.telemetry_stale_after_seconds)

    def _runtime_snapshot_locked(self) -> TelemetryRuntimeStatus:
        return TelemetryRuntimeStatus(
            transport=self._transport,
            connected=self._connected,
            fallback_active=self._fallback_active,
            last_message_at_iso=self._last_message_at.isoformat() if self._last_message_at else None,
            is_stale=self._is_stale_locked(),
            stale_after_seconds=float(self._settings.telemetry_stale_after_seconds),
            error=self._error,
        )

    def _notification_signature_locked(self) -> tuple[object, ...]:
        return (
            _power_signature(self._power),
            self._power_status.source,
            self._power_status.available,
            self._power_status.error,
            self._transport,
            self._connected,
            self._fallback_active,
            self._error,
        )


class ControlCoordinator:
    def __init__(
        self,
        settings: Settings,
        telemetry_hub: TelemetryHub,
        *,
        system_factory: SystemFactory = PumpControlSystem,
        plug_client_factory: PlugClientFactory | None = None,
        weather_client: OpenMeteoClient | None = None,
    ) -> None:
        self._settings = settings
        self._telemetry_hub = telemetry_hub
        self._system_factory = system_factory
        self._plug_client_factory = plug_client_factory
        self._weather_client = weather_client or OpenMeteoClient()
        self._subscribers: set[StatusSubscriber] = set()
        self._latest_payload: dict[str, object] | None = None
        self._latest_weather = DEFAULT_WEATHER
        self._weather_refreshed_monotonic = 0.0
        self._loop_snapshot = self._build_loop_snapshot()
        self._run_lock = asyncio.Lock()
        self._debounce_task: asyncio.Task[None] | None = None
        self._last_control_finished_monotonic = 0.0
        self._is_active = False
        self._telemetry_hub.subscribe(self._handle_telemetry_update)

    def subscribe(self, callback: StatusSubscriber) -> None:
        self._subscribers.add(callback)

    async def start(self) -> None:
        self._is_active = True
        await self._ensure_weather(force=True)
        await self._refresh_cached_payload(
            run_control=True,
            track_loop=True,
            broadcast=False,
            persist_metrics=True,
        )

    async def stop(self) -> None:
        self._is_active = False
        if self._debounce_task is not None:
            self._debounce_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._debounce_task
        self._loop_snapshot["is_active"] = False
        self._loop_snapshot["is_iteration_in_progress"] = False

    async def get_status_payload(self) -> dict[str, object]:
        if self._latest_payload is None:
            await self._refresh_cached_payload(
                run_control=False,
                track_loop=False,
                broadcast=False,
                persist_metrics=False,
            )
        return dict(self._latest_payload or {})

    async def run_manual_control(self) -> dict[str, object]:
        return await self._refresh_cached_payload(
            run_control=True,
            track_loop=True,
            broadcast=True,
            persist_metrics=True,
        )

    async def refresh_status(self, *, broadcast: bool) -> dict[str, object]:
        return await self._refresh_cached_payload(
            run_control=False,
            track_loop=False,
            broadcast=broadcast,
            persist_metrics=False,
        )

    async def _handle_telemetry_update(
        self,
        power: PowerSnapshot,
        power_status: TelemetryStatus,
        telemetry: TelemetryRuntimeStatus,
    ) -> None:
        del power
        del power_status
        if not self._is_active:
            return
        if telemetry.transport == "unavailable" or not telemetry.connected or telemetry.is_stale:
            await self.refresh_status(broadcast=True)
            return
        self._schedule_automatic_control()

    def _schedule_automatic_control(self) -> None:
        if self._debounce_task is not None:
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_control())

    async def _debounced_control(self) -> None:
        await asyncio.sleep(max(0.0, self._settings.policy_debounce_ms / 1000.0))
        elapsed = time.monotonic() - self._last_control_finished_monotonic
        min_interval = max(0.0, float(self._settings.policy_min_run_interval_seconds))
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        await self._refresh_cached_payload(
            run_control=True,
            track_loop=True,
            broadcast=True,
            persist_metrics=True,
        )

    async def _refresh_cached_payload(
        self,
        *,
        run_control: bool,
        track_loop: bool,
        broadcast: bool,
        persist_metrics: bool,
    ) -> dict[str, object]:
        async with self._run_lock:
            payload: dict[str, object] | None = None
            if track_loop:
                self._loop_snapshot["is_active"] = True
                self._loop_snapshot["is_iteration_in_progress"] = True
                self._loop_snapshot["interval_seconds"] = self._settings.control_interval_seconds
                self._loop_snapshot["last_started_at_iso"] = datetime.now(UTC).isoformat()

            try:
                await self._ensure_weather(force=False)
                power, power_status, telemetry = await self._telemetry_hub.get_state()
                system = self._system_factory(self._settings)

                should_actuate = run_control and power_status.available and telemetry.transport != "unavailable"
                _, payload = await self._execute_system_run(
                    system,
                    should_actuate=should_actuate,
                    power=power,
                    weather=self._latest_weather,
                    power_status=power_status,
                )

                if should_actuate:
                    self._loop_snapshot["last_actuation_status"] = payload.get("actuation", {}).get("status")
                elif track_loop and run_control:
                    self._loop_snapshot["last_actuation_status"] = "skipped_unavailable"
                if track_loop:
                    self._loop_snapshot["last_error"] = None

                payload["plug"] = await self._fetch_plug_status()
                payload["telemetry"] = telemetry.to_dict()

                if persist_metrics and should_actuate:
                    await asyncio.to_thread(db.insert_metrics, self._settings.database_file, payload)
                if run_control and track_loop:
                    self._last_control_finished_monotonic = time.monotonic()
            except Exception as exc:
                if track_loop:
                    self._loop_snapshot["last_error"] = str(exc) or exc.__class__.__name__
                logger.exception("Failed to refresh cached controller payload.")
                raise
            finally:
                if track_loop:
                    self._loop_snapshot["last_completed_at_iso"] = datetime.now(UTC).isoformat()
                    self._loop_snapshot["is_iteration_in_progress"] = False

            if payload is not None:
                payload["control_loop"] = dict(self._loop_snapshot)
                self._latest_payload = payload
            payload = dict(self._latest_payload or {})

        if broadcast:
            await self._notify_subscribers(payload)
        return payload

    async def _fetch_plug_status(self) -> dict[str, Any]:
        if not self._settings.shelly_host or self._plug_client_factory is None:
            return {
                "configured": False,
                "reachable": False,
                "error": "SHELLY_HOST is not configured.",
                "status": None,
            }

        try:
            status = await self._plug_client_factory(self._settings).fetch_switch_status()
            return {
                "configured": True,
                "reachable": True,
                "error": None,
                "status": status.to_dict(),
            }
        except ShellyError as exc:
            return {
                "configured": True,
                "reachable": False,
                "error": str(exc),
                "status": None,
            }

    async def _ensure_weather(self, *, force: bool) -> None:
        interval = max(1.0, float(self._settings.weather_refresh_seconds))
        if not force and self._weather_refreshed_monotonic > 0:
            age = time.monotonic() - self._weather_refreshed_monotonic
            if age < interval:
                return

        weather = WeatherSnapshot(
            current_temperature_c=None,
            today_min_temperature_c=None,
            today_max_temperature_c=None,
            weather_code=None,
            queried_timezone=self._settings.weather_timezone,
        )
        try:
            async with aiohttp.ClientSession() as session:
                weather = await self._weather_client.fetch_weather(
                    session=session,
                    latitude=self._settings.weather_latitude,
                    longitude=self._settings.weather_longitude,
                    timezone=self._settings.weather_timezone,
                )
        except aiohttp.ClientError:
            pass

        self._latest_weather = weather
        self._weather_refreshed_monotonic = time.monotonic()

    async def _notify_subscribers(self, payload: dict[str, object]) -> None:
        for callback in list(self._subscribers):
            await _invoke_callback(callback, payload)

    async def _execute_system_run(
        self,
        system: PumpControlSystem,
        *,
        should_actuate: bool,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        power_status: TelemetryStatus,
    ) -> tuple[object, dict[str, object]]:
        if should_actuate and hasattr(system, "control_with_inputs"):
            return await system.control_with_inputs(
                power=power,
                weather=weather,
                power_status=power_status,
            )
        if not should_actuate and hasattr(system, "evaluate_with_inputs"):
            return await system.evaluate_with_inputs(
                power=power,
                weather=weather,
                power_status=power_status,
            )
        if should_actuate:
            return await system.control()
        return await system.evaluate()

    @staticmethod
    def _build_loop_snapshot() -> dict[str, object]:
        return {
            "is_active": False,
            "is_iteration_in_progress": False,
            "interval_seconds": None,
            "last_started_at_iso": None,
            "last_completed_at_iso": None,
            "last_actuation_status": None,
            "last_error": None,
        }


async def _invoke_callback(callback: Callable[..., Awaitable[None] | None], *args: object) -> None:
    result = callback(*args)
    if asyncio.iscoroutine(result):
        await result


def _build_unavailable_snapshot(settings: Settings) -> PowerSnapshot:
    return PowerSnapshot.with_timestamp(
        site_id=settings.site_id or 0,
        site_name=settings.cerbo_site_name,
        site_identifier=settings.cerbo_site_identifier,
        battery_soc_percent=None,
        solar_watts=None,
        house_watts=None,
        house_l1_watts=None,
        house_l2_watts=None,
        house_l3_watts=None,
        generator_watts=None,
        active_input_source=None,
        queried_at_unix_ms=None,
    )


def _power_signature(power: PowerSnapshot) -> tuple[object, ...]:
    return (
        power.battery_soc_percent,
        power.solar_watts,
        power.house_watts,
        power.house_l1_watts,
        power.house_l2_watts,
        power.house_l3_watts,
        power.generator_watts,
        power.active_input_source,
        power.site_identifier,
    )


def _coerce_payload_bytes(payload: object | None) -> bytes | str | None:
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    return str(payload)


def _to_float(value: object | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
