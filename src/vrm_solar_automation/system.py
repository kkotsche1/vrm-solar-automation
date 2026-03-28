from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import aiohttp

from .client import ProbeUnavailableError, VrmProbeClient
from .config import Settings
from .models import PowerSnapshot
from .policy import PumpDecision, PumpPolicy, PumpPolicyState
from .shelly import ShellyError, ShellyPlugClient
from .state import StateStore
from .weather import OpenMeteoClient, WeatherSnapshot

OVERRIDE_MANUAL_ON_UNTIL = "manual_on_until"
OVERRIDE_MANUAL_OFF_UNTIL = "manual_off_until"
OVERRIDE_MANUAL_OFF_UNTIL_NEXT_AUTO_ON = "manual_off_until_next_auto_on"
OVERRIDE_EMERGENCY_OFF = "emergency_off"


@dataclass(frozen=True)
class PumpActuationResult:
    status: str
    intended_is_on: bool
    observed_before_is_on: bool | None
    observed_after_is_on: bool | None
    command_sent: str | None
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PumpOverrideResult:
    mode: str | None
    is_active: bool
    effective_target_is_on: bool
    reason: str | None
    until_iso: str | None
    seen_auto_off: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TelemetryStatus:
    source: str
    available: bool
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PumpControlSystem:
    def __init__(
        self,
        settings: Settings,
        *,
        policy: PumpPolicy | None = None,
        weather_client: OpenMeteoClient | None = None,
        probe_client: VrmProbeClient | None = None,
        plug_client: ShellyPlugClient | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy or PumpPolicy()
        self._weather_client = weather_client or OpenMeteoClient()
        self._probe_client = probe_client or VrmProbeClient(settings)
        self._plug_client = plug_client
        if self._plug_client is None and settings.shelly_host:
            self._plug_client = ShellyPlugClient(settings.shelly_settings())
        self._state_store = state_store or StateStore(settings.state_file)

    async def evaluate(self) -> tuple[PumpDecision, dict[str, object]]:
        decision, power, weather, previous_state, next_state, override, power_status = (
            await self._evaluate_policy()
        )

        return decision, self._build_payload(
            decision=decision,
            power=power.to_dict(),
            weather=weather.to_dict(),
            previous_state=previous_state,
            next_state=next_state,
            override=override,
            power_status=power_status,
        )

    async def control(self) -> tuple[PumpDecision, dict[str, object]]:
        decision, power, weather, previous_state, next_state, override, power_status = (
            await self._evaluate_policy()
        )

        self._state_store.save(next_state)
        actuation, final_state = await self._apply_intended_state(
            next_state,
            intended_is_on=override.effective_target_is_on,
        )
        self._state_store.save(final_state)

        payload = self._build_payload(
            decision=decision,
            power=power.to_dict(),
            weather=weather.to_dict(),
            previous_state=previous_state,
            next_state=final_state,
            override=override,
            power_status=power_status,
        )
        payload["actuation"] = actuation.to_dict()
        return decision, payload

    async def set_manual_on_override(self, *, duration_minutes: float) -> dict[str, object]:
        duration = _minutes_to_timedelta(duration_minutes)
        state = self._load_or_initialize_state()
        until = datetime.now(UTC) + duration
        updated_state = PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            last_known_plug_is_on=state.last_known_plug_is_on,
            last_known_plug_at_iso=state.last_known_plug_at_iso,
            last_actuation_error=state.last_actuation_error,
            last_actuation_at_iso=state.last_actuation_at_iso,
            override_mode=OVERRIDE_MANUAL_ON_UNTIL,
            override_until_iso=until.isoformat(),
            override_set_at_iso=datetime.now(UTC).isoformat(),
            override_seen_auto_off=False,
        )
        self._state_store.save(updated_state)
        actuation, final_state = await self._apply_intended_state(updated_state, intended_is_on=True)
        self._state_store.save(final_state)
        return {
            "override": self._override_result_from_state(final_state, effective_target_is_on=True).to_dict(),
            "actuation": actuation.to_dict(),
        }

    async def set_manual_off_override(self, *, duration_minutes: float) -> dict[str, object]:
        duration = _minutes_to_timedelta(duration_minutes)
        state = self._load_or_initialize_state()
        until = datetime.now(UTC) + duration
        updated_state = PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            last_known_plug_is_on=state.last_known_plug_is_on,
            last_known_plug_at_iso=state.last_known_plug_at_iso,
            last_actuation_error=state.last_actuation_error,
            last_actuation_at_iso=state.last_actuation_at_iso,
            override_mode=OVERRIDE_MANUAL_OFF_UNTIL,
            override_until_iso=until.isoformat(),
            override_set_at_iso=datetime.now(UTC).isoformat(),
            override_seen_auto_off=False,
        )
        self._state_store.save(updated_state)
        actuation, final_state = await self._apply_intended_state(updated_state, intended_is_on=False)
        self._state_store.save(final_state)
        return {
            "override": self._override_result_from_state(final_state, effective_target_is_on=False).to_dict(),
            "actuation": actuation.to_dict(),
        }

    async def set_manual_off_until_next_auto_on_override(self) -> dict[str, object]:
        state = self._load_or_initialize_state()
        updated_state = PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            last_known_plug_is_on=state.last_known_plug_is_on,
            last_known_plug_at_iso=state.last_known_plug_at_iso,
            last_actuation_error=state.last_actuation_error,
            last_actuation_at_iso=state.last_actuation_at_iso,
            override_mode=OVERRIDE_MANUAL_OFF_UNTIL_NEXT_AUTO_ON,
            override_until_iso=None,
            override_set_at_iso=datetime.now(UTC).isoformat(),
            override_seen_auto_off=False,
        )
        self._state_store.save(updated_state)
        actuation, final_state = await self._apply_intended_state(updated_state, intended_is_on=False)
        self._state_store.save(final_state)
        return {
            "override": self._override_result_from_state(final_state, effective_target_is_on=False).to_dict(),
            "actuation": actuation.to_dict(),
        }

    async def set_emergency_off_override(self) -> dict[str, object]:
        state = self._load_or_initialize_state()
        updated_state = PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            last_known_plug_is_on=state.last_known_plug_is_on,
            last_known_plug_at_iso=state.last_known_plug_at_iso,
            last_actuation_error=state.last_actuation_error,
            last_actuation_at_iso=state.last_actuation_at_iso,
            override_mode=OVERRIDE_EMERGENCY_OFF,
            override_until_iso=None,
            override_set_at_iso=datetime.now(UTC).isoformat(),
            override_seen_auto_off=False,
        )
        self._state_store.save(updated_state)
        actuation, final_state = await self._apply_intended_state(updated_state, intended_is_on=False)
        self._state_store.save(final_state)
        return {
            "override": self._override_result_from_state(final_state, effective_target_is_on=False).to_dict(),
            "actuation": actuation.to_dict(),
        }

    async def clear_override(self) -> dict[str, object]:
        state = self._load_or_initialize_state()
        cleared_state = self._clear_override(state)
        self._state_store.save(cleared_state)
        try:
            decision, payload = await self.control()
            return {
                "override": payload["override"],
                "actuation": payload["actuation"],
                "decision": decision.to_dict(),
            }
        except Exception as exc:
            return {
                "override": self._override_result_from_state(
                    cleared_state,
                    effective_target_is_on=cleared_state.is_on,
                ).to_dict(),
                "actuation": {
                    "status": "deferred",
                    "intended_is_on": None,
                    "observed_before_is_on": cleared_state.last_known_plug_is_on,
                    "observed_after_is_on": cleared_state.last_known_plug_is_on,
                    "command_sent": None,
                    "error": str(exc),
                },
                "decision": None,
            }

    def read_override(self) -> dict[str, object]:
        state = self._state_store.load()
        if state is None:
            return self._override_result_from_state(
                self._load_or_initialize_state(),
                effective_target_is_on=False,
            ).to_dict()
        return self._override_result_from_state(
            state,
            effective_target_is_on=state.is_on,
        ).to_dict()

    async def _evaluate_policy(
        self,
    ) -> tuple[
        PumpDecision,
        PowerSnapshot,
        WeatherSnapshot,
        PumpPolicyState | None,
        PumpPolicyState,
        PumpOverrideResult,
        TelemetryStatus,
    ]:
        power, power_status = await self._fetch_power()
        weather = await self._fetch_weather()

        previous_state = self._state_store.load()
        decision = self._policy.decide(
            power=power,
            weather=weather,
            previous_state=previous_state,
        )
        automatic_state = StateStore.from_decision(previous_state, decision.should_turn_on)
        next_state, override = self._apply_override(
            automatic_state,
            automatic_target_is_on=decision.should_turn_on,
            power=power,
        )

        return decision, power, weather, previous_state, next_state, override, power_status

    async def _fetch_power(self) -> tuple[PowerSnapshot, TelemetryStatus]:
        power_source = getattr(self._probe_client, "source", "cerbo_modbus")
        try:
            return (
                await self._probe_client.fetch_snapshot(),
                TelemetryStatus(
                    source=power_source,
                    available=True,
                    error=None,
                ),
            )
        except (ProbeUnavailableError, OSError, TimeoutError, RuntimeError) as exc:
            error_message = str(exc)
            if not isinstance(exc, ProbeUnavailableError):
                suffix = f" Details: {error_message}" if error_message else ""
                error_message = (
                    f"Unable to reach Cerbo GX at {self._settings.cerbo_host}:{self._settings.cerbo_port}."
                    f"{suffix}"
                )
            return (
                PowerSnapshot.with_timestamp(
                    site_id=self._settings.site_id or 0,
                    site_name=self._settings.cerbo_site_name,
                    site_identifier=self._settings.cerbo_site_identifier,
                    battery_soc_percent=None,
                    solar_watts=None,
                    house_watts=None,
                    generator_watts=None,
                    active_input_source=None,
                    queried_at_unix_ms=None,
                ),
                TelemetryStatus(
                    source=power_source,
                    available=False,
                    error=error_message,
                ),
            )

    async def _fetch_weather(self) -> WeatherSnapshot:
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
        return weather

    async def _apply_intended_state(
        self,
        state: PumpPolicyState,
        *,
        intended_is_on: bool,
    ) -> tuple[PumpActuationResult, PumpPolicyState]:
        if self._plug_client is None:
            return (
                PumpActuationResult(
                    status="skipped",
                    intended_is_on=intended_is_on,
                    observed_before_is_on=None,
                    observed_after_is_on=state.last_known_plug_is_on,
                    command_sent=None,
                    error=None,
                ),
                state,
            )

        observed_before = None
        observed_after = None
        command_sent = None
        error = None

        try:
            observed_before = (await self._plug_client.fetch_switch_status()).output
        except ShellyError as exc:
            error = str(exc)

        if observed_before is None or observed_before != intended_is_on:
            try:
                command_result = await (
                    self._plug_client.turn_on()
                    if intended_is_on
                    else self._plug_client.turn_off()
                )
                command_sent = "turn_on" if intended_is_on else "turn_off"
                observed_after = command_result.output
                error = None
            except ShellyError as exc:
                error = str(exc)

        try:
            observed_after = (await self._plug_client.fetch_switch_status()).output
        except ShellyError as exc:
            if error is None:
                error = str(exc)

        status = "already_aligned"
        if error is not None and observed_after is None and observed_before is None:
            status = "unreachable"
        elif command_sent is not None and observed_after == intended_is_on:
            status = "reconciled"
        elif command_sent is not None and observed_after is None:
            status = "command_sent_unverified"
        elif command_sent is not None:
            status = "mismatch_after_command"
        elif observed_before is None:
            status = "unknown"

        return (
            PumpActuationResult(
                status=status,
                intended_is_on=intended_is_on,
                observed_before_is_on=observed_before,
                observed_after_is_on=observed_after,
                command_sent=command_sent,
                error=error,
            ),
            self._merge_runtime_state(
                state,
                observed_is_on=observed_after if observed_after is not None else observed_before,
                error=error,
                mark_actuation=command_sent is not None or error is not None,
            ),
        )

    def _apply_override(
        self,
        state: PumpPolicyState,
        *,
        automatic_target_is_on: bool,
        power: PowerSnapshot,
    ) -> tuple[PumpPolicyState, PumpOverrideResult]:
        now = datetime.now(UTC)
        state = self._clear_expired_override(state, now)

        if state.override_mode is None:
            return (
                state,
                PumpOverrideResult(
                    mode=None,
                    is_active=False,
                    effective_target_is_on=automatic_target_is_on,
                    reason=None,
                    until_iso=None,
                    seen_auto_off=False,
                ),
            )

        if state.override_mode == OVERRIDE_MANUAL_ON_UNTIL:
            if self._battery_safety_forces_off(power):
                return (
                    state,
                    PumpOverrideResult(
                        mode=state.override_mode,
                        is_active=True,
                        effective_target_is_on=False,
                        reason="Manual on override is active, but battery safety still forces the pump off.",
                        until_iso=state.override_until_iso,
                        seen_auto_off=state.override_seen_auto_off,
                    ),
                )
            return (
                state,
                PumpOverrideResult(
                    mode=state.override_mode,
                    is_active=True,
                    effective_target_is_on=True,
                    reason="Manual on override is active until its timer expires.",
                    until_iso=state.override_until_iso,
                    seen_auto_off=state.override_seen_auto_off,
                ),
            )

        if state.override_mode == OVERRIDE_MANUAL_OFF_UNTIL:
            return (
                state,
                PumpOverrideResult(
                    mode=state.override_mode,
                    is_active=True,
                    effective_target_is_on=False,
                    reason="Manual off override is active until its timer expires.",
                    until_iso=state.override_until_iso,
                    seen_auto_off=state.override_seen_auto_off,
                ),
            )

        if state.override_mode == OVERRIDE_EMERGENCY_OFF:
            return (
                state,
                PumpOverrideResult(
                    mode=state.override_mode,
                    is_active=True,
                    effective_target_is_on=False,
                    reason="Emergency off is active until automatic control is manually restored.",
                    until_iso=None,
                    seen_auto_off=False,
                ),
            )

        seen_auto_off = state.override_seen_auto_off or not automatic_target_is_on
        if automatic_target_is_on and seen_auto_off:
            cleared_state = self._clear_override(state)
            return (
                cleared_state,
                PumpOverrideResult(
                    mode=None,
                    is_active=False,
                    effective_target_is_on=True,
                    reason="Manual off override was released by a fresh automatic ON signal.",
                    until_iso=None,
                    seen_auto_off=True,
                ),
            )

        updated_state = PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            last_known_plug_is_on=state.last_known_plug_is_on,
            last_known_plug_at_iso=state.last_known_plug_at_iso,
            last_actuation_error=state.last_actuation_error,
            last_actuation_at_iso=state.last_actuation_at_iso,
            override_mode=state.override_mode,
            override_until_iso=state.override_until_iso,
            override_set_at_iso=state.override_set_at_iso,
            override_seen_auto_off=seen_auto_off,
        )
        return (
            updated_state,
            PumpOverrideResult(
                mode=updated_state.override_mode,
                is_active=True,
                effective_target_is_on=False,
                reason="Manual off override is waiting for the next fresh automatic ON signal.",
                until_iso=updated_state.override_until_iso,
                seen_auto_off=updated_state.override_seen_auto_off,
            ),
        )

    def _merge_runtime_state(
        self,
        state: PumpPolicyState,
        *,
        observed_is_on: bool | None,
        error: str | None,
        mark_actuation: bool,
    ) -> PumpPolicyState:
        now_iso = datetime.now(UTC).isoformat()
        return PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            last_known_plug_is_on=(
                observed_is_on if observed_is_on is not None else state.last_known_plug_is_on
            ),
            last_known_plug_at_iso=(
                now_iso if observed_is_on is not None else state.last_known_plug_at_iso
            ),
            last_actuation_error=error,
            last_actuation_at_iso=(
                now_iso if mark_actuation else state.last_actuation_at_iso
            ),
            override_mode=state.override_mode,
            override_until_iso=state.override_until_iso,
            override_set_at_iso=state.override_set_at_iso,
            override_seen_auto_off=state.override_seen_auto_off,
        )

    def _build_payload(
        self,
        *,
        decision: PumpDecision,
        power: dict[str, object],
        weather: dict[str, object],
        previous_state: PumpPolicyState | None,
        next_state: PumpPolicyState,
        override: PumpOverrideResult,
        power_status: TelemetryStatus,
    ) -> dict[str, object]:
        return {
            "power": power,
            "power_status": power_status.to_dict(),
            "weather": weather,
            "previous_state": previous_state.to_dict() if previous_state else None,
            "next_state": next_state.to_dict(),
            "decision": decision.to_dict(),
            "override": override.to_dict(),
        }

    def _clear_expired_override(self, state: PumpPolicyState, now: datetime) -> PumpPolicyState:
        if state.override_mode not in {OVERRIDE_MANUAL_ON_UNTIL, OVERRIDE_MANUAL_OFF_UNTIL}:
            return state
        if state.override_until is None or now < state.override_until:
            return state
        return self._clear_override(state)

    def _clear_override(self, state: PumpPolicyState) -> PumpPolicyState:
        return PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            last_known_plug_is_on=state.last_known_plug_is_on,
            last_known_plug_at_iso=state.last_known_plug_at_iso,
            last_actuation_error=state.last_actuation_error,
            last_actuation_at_iso=state.last_actuation_at_iso,
            override_mode=None,
            override_until_iso=None,
            override_set_at_iso=None,
            override_seen_auto_off=False,
        )

    def _load_or_initialize_state(self) -> PumpPolicyState:
        state = self._state_store.load()
        if state is not None:
            return state
        return PumpPolicyState(
            is_on=False,
            changed_at_iso=datetime.now(UTC).isoformat(),
        )

    def _override_result_from_state(
        self,
        state: PumpPolicyState,
        *,
        effective_target_is_on: bool,
    ) -> PumpOverrideResult:
        return PumpOverrideResult(
            mode=state.override_mode,
            is_active=state.override_mode is not None,
            effective_target_is_on=effective_target_is_on,
            reason=None,
            until_iso=state.override_until_iso,
            seen_auto_off=state.override_seen_auto_off,
        )

    def _battery_safety_forces_off(self, power: PowerSnapshot) -> bool:
        battery_soc = power.battery_soc_percent
        if battery_soc is None:
            return True
        return battery_soc <= self._policy._config.battery_off_below_soc


def _minutes_to_timedelta(minutes: float) -> timedelta:
    normalized = float(minutes)
    if normalized <= 0:
        raise ValueError("Override duration must be greater than zero minutes.")
    return timedelta(minutes=normalized)
