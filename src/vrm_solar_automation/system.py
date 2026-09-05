from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
import logging
from typing import Protocol
from zoneinfo import ZoneInfo

import aiohttp

from .client import ProbeUnavailableError, VrmProbeClient
from .config import Settings
from .db import upgrade_database
from .models import PowerSnapshot
from .notifier import GmailSmtpNotifier
from .policy import (
    PumpDecision,
    PumpPolicy,
    PumpPolicyConfig,
    PumpPolicyState,
    forecast_liberal_factor,
    linear_interpolate,
)
from .shelly import ShellyError, ShellyPlugClient
from .state import StateStore
from .weather import OpenMeteoClient, WeatherSnapshot

QUIET_HOURS_BLOCK_REASON = "Pump operation is forced off during configured quiet hours."
LOGGER = logging.getLogger(__name__)


class StateRepository(Protocol):
    def load(self) -> PumpPolicyState | None: ...
    def save(self, state: PumpPolicyState) -> None: ...
    def record_control_cycle(
        self,
        *,
        timestamp_unix_ms: int,
        timestamp_iso: str,
        power: dict[str, object],
        weather: dict[str, object],
        weather_source: str,
        power_status: dict[str, object],
        decision: PumpDecision,
        intended_target_is_on: bool,
        quiet_hours_blocked: bool,
        blocked_reason: str | None,
        actuation: dict[str, object],
        plug_observed_is_on: bool | None = None,
        policy_fingerprint: dict[str, object] | None = None,
    ) -> None: ...


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
class TelemetryStatus:
    source: str
    available: bool
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class WeatherFetchResult:
    snapshot: WeatherSnapshot
    source: str


class PumpControlSystem:
    def __init__(
        self,
        settings: Settings,
        *,
        policy: PumpPolicy | None = None,
        weather_client: OpenMeteoClient | None = None,
        probe_client: VrmProbeClient | None = None,
        plug_client: ShellyPlugClient | None = None,
        state_store: StateRepository | None = None,
        notifier: GmailSmtpNotifier | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        if settings.database_auto_migrate:
            upgrade_database(settings.database_url)
        policy_config = PumpPolicyConfig(
            battery_min_soc=settings.battery_min_soc_percent,
            battery_soft_min_soc=settings.battery_soft_min_soc_percent,
            battery_hard_min_soc=settings.battery_hard_min_soc_percent,
            sunshine_hours_min=settings.sunshine_hours_min,
            forecast_liberal_sunshine_hours_min=settings.forecast_liberal_sunshine_hours_min,
            forecast_liberal_sunshine_hours_max=settings.forecast_liberal_sunshine_hours_max,
            battery_alert_soc_percents=settings.battery_alert_soc_percents,
            battery_alert_rearm_margin_percent=settings.battery_alert_rearm_margin_percent,
            daytime_surplus_turn_on_enabled=settings.daytime_surplus_turn_on_enabled,
            daytime_surplus_pump_load_kw=settings.surplus_night_pump_load_kw,
            daytime_surplus_margin_kw=settings.daytime_surplus_margin_kw,
            # Same landing point the overnight reserve is budgeted to, so the daytime
            # override cannot spend the reserve the night policy just protected.
            daytime_surplus_floor_soc=(
                settings.battery_hard_min_soc_percent
                + settings.surplus_night_generator_margin_soc_percent
            ),
        )
        self._policy = policy or PumpPolicy(policy_config)
        self._generator_alert_threshold_watts = policy_config.generator_alert_watts
        self._generator_block_start_watts = settings.generator_block_start_watts
        self._battery_alert_soc_percents = policy_config.battery_alert_soc_percents
        self._battery_alert_rearm_margin_percent = policy_config.battery_alert_rearm_margin_percent
        self._weather_client = weather_client or OpenMeteoClient()
        self._probe_client = probe_client or VrmProbeClient(settings)
        self._plug_client = plug_client
        if self._plug_client is None and settings.shelly_host:
            self._plug_client = ShellyPlugClient(settings.shelly_settings())
        self._state_store: StateRepository = state_store or StateStore(settings.database_url)
        if notifier is not None:
            self._notifier = notifier
        elif settings.smtp_gmail_app_password:
            self._notifier = GmailSmtpNotifier(
                sender=settings.smtp_gmail_sender,
                app_password=settings.smtp_gmail_app_password,
                recipients=settings.smtp_gmail_recipients,
                display_timezone=settings.auto_control_timezone,
            )
        else:
            self._notifier = None
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._auto_off_start_minutes = _hhmm_to_minutes(settings.auto_off_start_local)
        self._auto_resume_start_minutes = _hhmm_to_minutes(settings.auto_resume_start_local)
        self._auto_control_timezone = ZoneInfo(settings.auto_control_timezone)
        self._auto_resume_follows_crossover = settings.auto_resume_follows_crossover
        self._weather_timezone = ZoneInfo(settings.weather_timezone)
        self._surplus_night_enabled = settings.surplus_night_enabled
        self._surplus_night_base_load_kw = settings.surplus_night_base_load_kw
        self._surplus_night_morning_base_load_kw = settings.surplus_night_morning_base_load_kw
        self._morning_start_minutes = _hhmm_to_minutes(settings.surplus_night_morning_start_local)
        self._solar_crossover_fallback_minutes = _hhmm_to_minutes(
            settings.surplus_night_solar_crossover_local
        )
        self._crossover_after_sunrise_minutes = (
            settings.surplus_night_crossover_after_sunrise_minutes
        )
        self._surplus_night_generator_margin_soc_percent = (
            settings.surplus_night_generator_margin_soc_percent
        )
        self._surplus_night_turn_on_margin_soc_percent = (
            settings.surplus_night_turn_on_margin_soc_percent
        )
        self._surplus_night_next_day_sunshine_min = settings.surplus_night_next_day_sunshine_min
        self._surplus_night_pump_load_kw = settings.surplus_night_pump_load_kw
        self._surplus_night_min_run_hours = settings.surplus_night_min_run_hours
        self._battery_capacity_kwh = settings.battery_capacity_kwh
        self._battery_hard_min_soc_percent = settings.battery_hard_min_soc_percent
        self._forecast_liberal_sunshine_hours_min = settings.forecast_liberal_sunshine_hours_min
        self._forecast_liberal_sunshine_hours_max = settings.forecast_liberal_sunshine_hours_max
        self._cerbo_fetch_retry_count = settings.cerbo_fetch_retry_count
        self._cerbo_fetch_retry_delay_seconds = settings.cerbo_fetch_retry_delay_seconds
        self._cerbo_unavailable_grace_cycles = settings.cerbo_unavailable_grace_cycles
        self._weather_cache_date: date | None = None
        self._weather_cache_snapshot: WeatherSnapshot | None = None

    async def evaluate(self) -> tuple[PumpDecision, dict[str, object]]:
        power, power_status = await self._fetch_power()
        weather = await self._fetch_weather()
        return await self.evaluate_with_inputs(
            power=power,
            weather=weather.snapshot,
            power_status=power_status,
            weather_source=weather.source,
        )

    async def control(self) -> tuple[PumpDecision, dict[str, object]]:
        power, power_status = await self._fetch_power()
        weather = await self._fetch_weather()
        return await self.control_with_inputs(
            power=power,
            weather=weather.snapshot,
            power_status=power_status,
            weather_source=weather.source,
        )

    async def evaluate_with_inputs(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        power_status: TelemetryStatus,
        weather_source: str = "live",
    ) -> tuple[PumpDecision, dict[str, object]]:
        (
            decision,
            previous_state,
            next_state,
            quiet_hours_forced_off,
        ) = self._evaluate_controller_state(
            power=power,
            weather=weather,
            power_status=power_status,
        )
        intended_is_on = self._intended_target_is_on(next_state.is_on, quiet_hours_forced_off)
        quiet_hours_blocked = quiet_hours_forced_off and next_state.is_on

        return decision, self._build_payload(
            decision=decision,
            power=power.to_dict(),
            weather=weather.to_dict(),
            previous_state=previous_state,
            next_state=next_state,
            intended_is_on=intended_is_on,
            quiet_hours_blocked=quiet_hours_blocked,
            power_status=power_status,
            weather_source=weather_source,
        )

    async def control_with_inputs(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        power_status: TelemetryStatus,
        force_apply: bool = False,
        weather_source: str = "live",
    ) -> tuple[PumpDecision, dict[str, object]]:
        (
            decision,
            previous_state,
            next_state,
            quiet_hours_forced_off,
        ) = self._evaluate_controller_state(
            power=power,
            weather=weather,
            power_status=power_status,
        )
        intended_is_on = self._intended_target_is_on(next_state.is_on, quiet_hours_forced_off)
        quiet_hours_blocked = quiet_hours_forced_off and next_state.is_on
        previous_intended_is_on = self._previous_intended_target_is_on(previous_state)
        target_changed = (
            force_apply
            or previous_state is None
            or previous_intended_is_on != intended_is_on
        )

        self._state_store.save(next_state)
        actuation, final_state = await self._apply_intended_state(
            next_state,
            intended_is_on=intended_is_on,
            quiet_hours_active=quiet_hours_forced_off,
            quiet_hours_blocked=quiet_hours_blocked,
            target_changed=target_changed,
            decision_action=decision.action,
            decision_reason=decision.reason,
        )
        final_state = self._apply_plug_mismatch_alert_state(
            final_state,
            actuation=actuation,
            intended_is_on=intended_is_on,
            decision_action=decision.action,
            decision_reason=decision.reason,
        )
        final_state = self._apply_alert_state(
            final_state,
            power=power,
            power_status=power_status,
            decision=decision,
            weather=weather,
        )
        final_state = self._with_weather_cache(
            final_state,
            weather=weather,
            weather_source=weather_source,
        )
        self._state_store.save(final_state)

        payload = self._build_payload(
            decision=decision,
            power=power.to_dict(),
            weather=weather.to_dict(),
            previous_state=previous_state,
            next_state=final_state,
            intended_is_on=intended_is_on,
            quiet_hours_blocked=quiet_hours_blocked,
            power_status=power_status,
            weather_source=weather_source,
        )
        payload["actuation"] = actuation.to_dict()
        cycle_timestamp = datetime.now(UTC)
        self._state_store.record_control_cycle(
            timestamp_unix_ms=int(cycle_timestamp.timestamp() * 1000),
            timestamp_iso=cycle_timestamp.isoformat(),
            power=payload["power"],
            weather=payload["weather"],
            weather_source=weather_source,
            power_status=payload["power_status"],
            decision=decision,
            intended_target_is_on=bool(payload["intended_target_is_on"]),
            quiet_hours_blocked=bool(payload["quiet_hours_blocked"]),
            blocked_reason=payload["blocked_reason"],
            actuation=payload["actuation"],
            plug_observed_is_on=self._observed_plug_state(actuation),
            policy_fingerprint=self._policy_fingerprint(),
        )
        return decision, payload

    def _evaluate_controller_state(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        power_status: TelemetryStatus,
    ) -> tuple[
        PumpDecision,
        PumpPolicyState | None,
        PumpPolicyState,
        bool,
    ]:
        previous_state = self._state_store.load()
        if power_status.available:
            return self._evaluate_policy_with_inputs(
                power=power,
                weather=weather,
                previous_state=previous_state,
                power_status=power_status,
            )
        return self._evaluate_unavailable_power(
            previous_state=previous_state,
            power_status=power_status,
            weather=weather,
        )

    def _evaluate_policy_with_inputs(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        previous_state: PumpPolicyState | None,
        power_status: TelemetryStatus,
    ) -> tuple[
        PumpDecision,
        PumpPolicyState | None,
        PumpPolicyState,
        bool,
    ]:
        quiet_hours_forced_off = False
        local_now = self._local_now()
        decision = self._policy.decide(
            power=power,
            weather=weather,
            previous_state=previous_state,
        )
        if self._is_within_quiet_hours(local_now=local_now, weather=weather):
            if self._surplus_night_enabled:
                decision = self._decide_surplus_night(
                    power=power,
                    weather=weather,
                    previous_state=previous_state,
                    local_now=local_now,
                )
            else:
                quiet_hours_forced_off = True
        decision = self._apply_generator_start_guard(
            decision, power=power, previous_state=previous_state
        )
        next_state = StateStore.from_decision(
            previous_state,
            decision.should_turn_on,
            quiet_hours_forced_off=quiet_hours_forced_off,
        )
        next_state = self._with_power_status(next_state, power_status=power_status)
        return decision, previous_state, next_state, quiet_hours_forced_off

    def _apply_generator_start_guard(
        self,
        decision: PumpDecision,
        *,
        power: PowerSnapshot,
        previous_state: PumpPolicyState | None,
    ) -> PumpDecision:
        """Refuse to *start* the pump while the generator is running.

        Starting here spends generator fuel on a compressor run and holds the generator up
        under load, which is the opposite of what every reserve in this controller is for.
        Observed 2026-09-04: the generator ran unattended at 22:08 with the pump already
        off, and night control started the pump at 22:48 on the charge it had just made.

        Only starts are blocked. A pump already running has committed its compressor cycle,
        and stopping it mid-run would cycle the plug without recovering the energy.
        """
        if self._generator_block_start_watts <= 0:
            return decision
        if not decision.should_turn_on:
            return decision
        if previous_state is not None and previous_state.is_on:
            return decision
        generator_watts = power.generator_watts
        if generator_watts is None:
            return decision
        if abs(generator_watts) < self._generator_block_start_watts:
            return decision
        reason = (
            f"The generator is running at {abs(generator_watts):.0f} W, so the pump is not "
            "started: it would spend generator charge on a compressor run and hold the "
            "generator under load. The pump may start once the generator stops."
        )
        return replace(
            decision,
            should_turn_on=False,
            action=PumpPolicy._action(False, previous_state),
            reason=reason,
            reasons=[*decision.reasons, reason],
            generator_start_blocked=True,
        )

    async def _fetch_power(self) -> tuple[PowerSnapshot, TelemetryStatus]:
        power_source = getattr(self._probe_client, "source", "cerbo_modbus")
        attempts = max(1, self._cerbo_fetch_retry_count + 1)
        last_error = None
        for attempt in range(1, attempts + 1):
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
                last_error = self._format_power_error(exc)
                LOGGER.warning(
                    "Cerbo power fetch attempt %s/%s failed: %s",
                    attempt,
                    attempts,
                    last_error,
                )
                if attempt < attempts and self._cerbo_fetch_retry_delay_seconds > 0:
                    await asyncio.sleep(self._cerbo_fetch_retry_delay_seconds)

        return (
            self._unavailable_power_snapshot(),
            TelemetryStatus(
                source=power_source,
                available=False,
                error=last_error,
            ),
        )

    def _evaluate_unavailable_power(
        self,
        *,
        previous_state: PumpPolicyState | None,
        power_status: TelemetryStatus,
        weather: WeatherSnapshot | None = None,
    ) -> tuple[
        PumpDecision,
        PumpPolicyState | None,
        PumpPolicyState,
        bool,
    ]:
        local_now = self._local_now()
        quiet_hours_forced_off = (
            self._is_within_quiet_hours(local_now=local_now, weather=weather)
            and not self._surplus_night_enabled
        )
        next_failure_count = (previous_state.consecutive_power_failures if previous_state else 0) + 1
        previous_target_is_on = previous_state.is_on if previous_state is not None else False
        grace_limit = max(0, self._cerbo_unavailable_grace_cycles)

        if previous_target_is_on and next_failure_count < grace_limit:
            reason = (
                "Cerbo telemetry is unavailable after retries; holding the previous automatic ON "
                f"target (failure {next_failure_count}/{grace_limit})."
            )
            decision = self._telemetry_decision(
                target_on=True,
                previous_state=previous_state,
                reason=reason,
            )
        elif previous_target_is_on and grace_limit > 0 and next_failure_count >= grace_limit:
            reason = (
                "Cerbo telemetry is unavailable after retries, and the failure count reached "
                f"{next_failure_count}/{grace_limit}, so the pump fails safe to off."
            )
            decision = self._telemetry_decision(
                target_on=False,
                previous_state=previous_state,
                reason=reason,
            )
        else:
            reason = "Cerbo telemetry is unavailable after retries, so the controller keeps the pump off."
            decision = self._telemetry_decision(
                target_on=False,
                previous_state=previous_state,
                reason=reason,
            )

        next_state = StateStore.from_decision(
            previous_state,
            decision.should_turn_on,
            quiet_hours_forced_off=quiet_hours_forced_off,
        )
        next_state = self._with_power_status(next_state, power_status=power_status)
        return decision, previous_state, next_state, quiet_hours_forced_off

    def _telemetry_decision(
        self,
        *,
        target_on: bool,
        previous_state: PumpPolicyState | None,
        reason: str,
    ) -> PumpDecision:
        return PumpDecision(
            should_turn_on=target_on,
            action=self._action(target_on=target_on, previous_state=previous_state),
            reason=reason,
            reasons=[reason],
            weather_mode="telemetry_unavailable",
            soc_control_mode="telemetry_hold",
        )

    def _with_power_status(
        self,
        state: PumpPolicyState,
        *,
        power_status: TelemetryStatus,
    ) -> PumpPolicyState:
        if power_status.available:
            return replace(
                state,
                consecutive_power_failures=0,
            )

        return replace(
            state,
            consecutive_power_failures=state.consecutive_power_failures + 1,
            last_power_failure_at_iso=datetime.now(UTC).isoformat(),
            last_power_failure_error=power_status.error,
        )

    @staticmethod
    def _action(*, target_on: bool, previous_state: PumpPolicyState | None) -> str:
        if previous_state is None:
            return "turn_on" if target_on else "turn_off"
        if previous_state.is_on == target_on:
            return "keep_on" if target_on else "keep_off"
        return "turn_on" if target_on else "turn_off"

    def _unavailable_power_snapshot(self) -> PowerSnapshot:
        return PowerSnapshot.with_timestamp(
            site_id=self._settings.site_id or 0,
            site_name=self._settings.cerbo_site_name,
            site_identifier=self._settings.cerbo_site_identifier,
            battery_soc_percent=None,
            solar_watts=None,
            house_watts=None,
            generator_watts=None,
            active_input_source=None,
            queried_at_unix_ms=None,
        )

    def _format_power_error(self, exc: Exception) -> str:
        error_message = str(exc)
        if isinstance(exc, ProbeUnavailableError):
            return error_message
        suffix = f" Details: {error_message}" if error_message else ""
        return (
            f"Unable to reach Cerbo GX at {self._settings.cerbo_host}:{self._settings.cerbo_port}."
            f"{suffix}"
        )

    async def _fetch_weather(self) -> WeatherFetchResult:
        today = self._weather_local_date()
        if self._weather_cache_date == today and self._weather_cache_snapshot is not None:
            return WeatherFetchResult(snapshot=self._weather_cache_snapshot, source="same_day_cache")

        unavailable_weather = WeatherSnapshot(
            current_temperature_c=None,
            today_min_temperature_c=None,
            today_max_temperature_c=None,
            today_sunshine_hours=None,
            weather_code=None,
            queried_timezone=self._settings.weather_timezone,
            tomorrow_sunshine_hours=None,
        )
        try:
            async with aiohttp.ClientSession() as session:
                weather = await self._weather_client.fetch_weather(
                    session=session,
                    latitude=self._settings.weather_latitude,
                    longitude=self._settings.weather_longitude,
                    timezone=self._settings.weather_timezone,
                )
            self._weather_cache_date = today
            self._weather_cache_snapshot = weather
            return WeatherFetchResult(snapshot=weather, source="live")
        except (aiohttp.ClientError, TimeoutError) as exc:
            cached_weather = self._load_same_day_weather_cache(today)
            if cached_weather is not None:
                self._weather_cache_date = today
                self._weather_cache_snapshot = cached_weather
                LOGGER.warning(
                    "Weather fetch failed; reusing cached same-day forecast: %s",
                    exc,
                )
                return WeatherFetchResult(snapshot=cached_weather, source="same_day_cache")
            LOGGER.warning(
                "Weather fetch failed; no usable same-day forecast cache is available: %s",
                exc,
            )
            return WeatherFetchResult(snapshot=unavailable_weather, source="unavailable")

    async def _apply_intended_state(
        self,
        state: PumpPolicyState,
        *,
        intended_is_on: bool,
        quiet_hours_active: bool = False,
        quiet_hours_blocked: bool = False,
        target_changed: bool = True,
        decision_action: str,
        decision_reason: str,
    ) -> tuple[PumpActuationResult, PumpPolicyState]:
        if self._plug_client is None:
            status = (
                "blocked_quiet_hours"
                if quiet_hours_blocked and target_changed
                else "no_target_change"
                if not target_changed
                else "skipped"
            )
            return (
                PumpActuationResult(
                    status=status,
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

        observed_mismatch = observed_before is not None and observed_before != intended_is_on
        should_force_reconcile = quiet_hours_active and observed_before is True and not intended_is_on
        # Reassert the automatic target as soon as the plug is reachable again and visibly drifted.
        should_reconcile = target_changed or should_force_reconcile or observed_mismatch

        if not should_reconcile:
            status = "unreachable" if error is not None and observed_before is None else "no_target_change"
            return (
                PumpActuationResult(
                    status=status,
                    intended_is_on=intended_is_on,
                    observed_before_is_on=observed_before,
                    observed_after_is_on=observed_before,
                    command_sent=None,
                    error=error,
                ),
                self._merge_runtime_state(
                    state,
                    observed_is_on=observed_before,
                    error=error,
                    mark_actuation=False,
                ),
            )

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
        elif quiet_hours_blocked and not intended_is_on:
            status = "blocked_quiet_hours"

        actuation = PumpActuationResult(
            status=status,
            intended_is_on=intended_is_on,
            observed_before_is_on=observed_before,
            observed_after_is_on=observed_after,
            command_sent=command_sent,
            error=error,
        )

        if (
            command_sent is not None
            and decision_action not in ("keep_on", "keep_off")
            and self._notifier is not None
        ):
            try:
                self._notifier.send_plug_state_change_email(
                    command_sent=command_sent,
                    decision_action=decision_action,
                    decision_reason=decision_reason,
                    intended_is_on=intended_is_on,
                    actuation_status=actuation.status,
                    observed_before_is_on=actuation.observed_before_is_on,
                    observed_after_is_on=actuation.observed_after_is_on,
                    at_iso=datetime.now(UTC).isoformat(),
                )
            except Exception as exc:  # pragma: no cover - defensive logging path
                LOGGER.warning("Failed to send state-change email: %s", exc)

        return (
            actuation,
            self._merge_runtime_state(
                state,
                observed_is_on=observed_after if observed_after is not None else observed_before,
                error=error,
                mark_actuation=command_sent is not None or error is not None,
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
        return replace(
            state,
            last_known_plug_is_on=(
                observed_is_on if observed_is_on is not None else state.last_known_plug_is_on
            ),
            last_known_plug_at_iso=(
                now_iso if observed_is_on is not None else state.last_known_plug_at_iso
            ),
            last_actuation_error=error,
            last_actuation_at_iso=(now_iso if mark_actuation else state.last_actuation_at_iso),
        )

    def _apply_plug_mismatch_alert_state(
        self,
        state: PumpPolicyState,
        *,
        actuation: PumpActuationResult,
        intended_is_on: bool,
        decision_action: str,
        decision_reason: str,
    ) -> PumpPolicyState:
        observed_is_on = actuation.observed_after_is_on
        mismatch_alert_sent = state.plug_mismatch_alert_sent

        if observed_is_on is not None and observed_is_on != intended_is_on:
            if not mismatch_alert_sent:
                mismatch_alert_sent = self._send_plug_state_mismatch_alert(
                    intended_is_on=intended_is_on,
                    observed_is_on=observed_is_on,
                    decision_action=decision_action,
                    decision_reason=decision_reason,
                    actuation_status=actuation.status,
                )
        elif observed_is_on is not None:
            mismatch_alert_sent = False

        return replace(
            state,
            plug_mismatch_alert_sent=mismatch_alert_sent,
        )

    def _send_plug_state_mismatch_alert(
        self,
        *,
        intended_is_on: bool,
        observed_is_on: bool,
        decision_action: str,
        decision_reason: str,
        actuation_status: str,
    ) -> bool:
        if self._notifier is None:
            return False
        try:
            self._notifier.send_plug_state_mismatch_email(
                at_iso=datetime.now(UTC).isoformat(),
                intended_is_on=intended_is_on,
                observed_is_on=observed_is_on,
                decision_action=decision_action,
                decision_reason=decision_reason,
                actuation_status=actuation_status,
            )
        except Exception as exc:  # pragma: no cover - defensive logging path
            LOGGER.warning("Failed to send plug-state mismatch email: %s", exc)
            return False
        return True

    def _apply_alert_state(
        self,
        state: PumpPolicyState,
        *,
        power: PowerSnapshot,
        power_status: TelemetryStatus,
        decision: PumpDecision,
        weather: WeatherSnapshot,
    ) -> PumpPolicyState:
        battery_soc = power.battery_soc_percent if power_status.available else None
        generator_watts = power.generator_watts if power_status.available else None
        battery_alert_latched_percents = state.battery_alert_latched_percents
        generator_running_alert_sent = state.generator_running_alert_sent

        # Telemetry gaps leave every latch untouched: an unreachable Cerbo must not
        # re-arm alerts that were already sent for the current discharge.
        if battery_soc is not None:
            battery_alert_latched_percents, crossed_thresholds = self._battery_alert_latches(
                battery_soc=battery_soc,
                latched_percents=battery_alert_latched_percents,
            )
            if crossed_thresholds:
                self._send_battery_alert(
                    battery_soc_percent=battery_soc,
                    crossed_thresholds=crossed_thresholds,
                )

        if generator_watts is not None:
            if abs(generator_watts) >= self._generator_alert_threshold_watts:
                if not generator_running_alert_sent:
                    self._send_generator_started_alert(generator_watts=abs(generator_watts))
                    generator_running_alert_sent = True
            else:
                generator_running_alert_sent = False

        weather_block_alert_sent_local_date = state.weather_block_alert_sent_local_date
        weather_local_date = self._weather_local_date().isoformat()
        if (
            self._notifier is not None
            and self._decision_is_weather_blocked(decision=decision)
            and weather_block_alert_sent_local_date != weather_local_date
        ):
            self._send_weather_blocked_alert(
                decision=decision,
                weather=weather,
                local_date=weather_local_date,
            )
            weather_block_alert_sent_local_date = weather_local_date

        return replace(
            state,
            battery_alert_latched_percents=battery_alert_latched_percents,
            generator_running_alert_sent=generator_running_alert_sent,
            weather_block_alert_sent_local_date=weather_block_alert_sent_local_date,
        )

    def _send_battery_alert(
        self,
        *,
        battery_soc_percent: float | None,
        crossed_thresholds: tuple[float, ...],
    ) -> None:
        if self._notifier is None or battery_soc_percent is None:
            return
        try:
            self._notifier.send_battery_alert_email(
                battery_soc_percent=battery_soc_percent,
                crossed_thresholds=crossed_thresholds,
                at_iso=datetime.now(UTC).isoformat(),
            )
        except Exception as exc:  # pragma: no cover - defensive logging path
            LOGGER.warning("Failed to send battery alert email: %s", exc)

    def _send_generator_started_alert(self, *, generator_watts: float) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.send_generator_started_email(
                generator_watts=generator_watts,
                at_iso=datetime.now(UTC).isoformat(),
            )
        except Exception as exc:  # pragma: no cover - defensive logging path
            LOGGER.warning("Failed to send generator alert email: %s", exc)

    def _send_weather_blocked_alert(
        self,
        *,
        decision: PumpDecision,
        weather: WeatherSnapshot,
        local_date: str,
    ) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.send_weather_blocked_alert_email(
                at_iso=datetime.now(UTC).isoformat(),
                local_date=local_date,
                weather_mode=decision.weather_mode,
                decision_reason=decision.reason,
                today_sunshine_hours=weather.today_sunshine_hours,
                tomorrow_sunshine_hours=weather.tomorrow_sunshine_hours,
                night_reference_sunshine_hours=decision.night_reference_sunshine_hours,
            )
        except Exception as exc:  # pragma: no cover - defensive logging path
            LOGGER.warning("Failed to send weather-block alert email: %s", exc)

    def _decision_is_weather_blocked(self, *, decision: PumpDecision) -> bool:
        if decision.should_turn_on:
            return False

        reason = decision.reason.lower()
        if decision.weather_mode in {"insufficient_sun", "unknown"}:
            return (
                "automatic demand is off" in reason
                or "automatic control stays off" in reason
            )

        if decision.weather_mode == "surplus_night":
            return (
                "sunshine-hours forecast is unavailable" in reason
                or "surplus-night minimum" in reason
            )

        return False

    def _battery_alert_latches(
        self,
        *,
        battery_soc: float,
        latched_percents: tuple[float, ...],
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """Return the next latch set plus the thresholds newly crossed this cycle.

        A threshold alerts once when SOC reaches it and stays latched — including
        while SOC keeps falling — until SOC recovers past the re-arm margin above it.
        """
        margin = self._battery_alert_rearm_margin_percent
        crossed: list[float] = []
        still_latched: list[float] = []

        for threshold in self._battery_alert_soc_percents:
            was_latched = _contains_percent(latched_percents, threshold)
            if battery_soc <= threshold:
                if not was_latched:
                    crossed.append(threshold)
                still_latched.append(threshold)
            elif was_latched and battery_soc < threshold + margin:
                still_latched.append(threshold)

        return tuple(still_latched), tuple(crossed)

    def _decide_surplus_night(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        previous_state: PumpPolicyState | None,
        local_now: datetime,
    ) -> PumpDecision:
        battery_soc = power.battery_soc_percent
        required_soc = self._required_night_soc_percent(local_now=local_now, weather=weather)
        pump_reserve = self._pump_start_reserve_soc_percent(
            local_now=local_now, weather=weather
        )
        crossover_minutes, crossover_source = self._solar_crossover_minutes(
            weather=weather, local_now=local_now
        )
        crossover_local = f"{crossover_minutes // 60:02d}:{crossover_minutes % 60:02d}"
        reference_label, reference_sunshine = self._night_reference_sunshine(
            weather=weather,
            local_now=local_now,
        )
        previous_target_is_on = previous_state.is_on if previous_state is not None else False
        liberal_factor = forecast_liberal_factor(
            reference_sunshine,
            liberal_sunshine_hours_min=self._forecast_liberal_sunshine_hours_min,
            liberal_sunshine_hours_max=self._forecast_liberal_sunshine_hours_max,
        )
        # One rule: run until the battery holds exactly what base load needs to reach solar
        # crossover on the night floor, then stop. Nothing else limits the run, because
        # nothing else has to -- the reserve already accounts for every hour left. A start
        # additionally carries the compressor run it commits to, since that draw happens
        # whatever SOC does next, plus a small margin so a start is worth making.
        turn_off_threshold = required_soc
        turn_on_threshold = (
            required_soc + pump_reserve + self._surplus_night_turn_on_margin_soc_percent
        )
        blocked_subject = "Reserve-aware night control"
        mode_phrase = ""
        reserve_clause = (
            f" (including {pump_reserve:.1f}% for the compressor run a start commits to)"
        )

        if battery_soc is None:
            return self._night_decision(
                target_on=False,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                effective_turn_on_soc_percent=turn_on_threshold,
                effective_turn_off_soc_percent=turn_off_threshold,
                forecast_liberal_factor=liberal_factor,
                pump_reserve_soc_percent=pump_reserve,
                solar_crossover_local=crossover_local,
                solar_crossover_source=crossover_source,
                reason="Battery SOC is unavailable, so reserve-aware night control fails safe to off.",
            )

        if reference_sunshine is None:
            return self._night_decision(
                target_on=False,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                effective_turn_on_soc_percent=None,
                effective_turn_off_soc_percent=None,
                forecast_liberal_factor=None,
                pump_reserve_soc_percent=pump_reserve,
                solar_crossover_local=crossover_local,
                solar_crossover_source=crossover_source,
                reason=(
                    f"{reference_label.capitalize()}'s sunshine-hours forecast is unavailable, so "
                    "reserve-aware night control keeps the pump off."
                ),
            )

        if reference_sunshine < self._surplus_night_next_day_sunshine_min:
            return self._night_decision(
                target_on=False,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                effective_turn_on_soc_percent=turn_on_threshold,
                effective_turn_off_soc_percent=turn_off_threshold,
                forecast_liberal_factor=liberal_factor,
                pump_reserve_soc_percent=pump_reserve,
                solar_crossover_local=crossover_local,
                solar_crossover_source=crossover_source,
                reason=(
                    f"{reference_label.capitalize()}'s sunshine forecast is {reference_sunshine:.1f} hours, "
                    f"below the {self._surplus_night_next_day_sunshine_min:.1f}-hour surplus-night minimum, "
                    "so the pump stays off."
                ),
            )

        if previous_target_is_on:
            if battery_soc <= turn_off_threshold:
                return self._night_decision(
                    target_on=False,
                    previous_state=previous_state,
                    required_soc=required_soc,
                    reference_sunshine=reference_sunshine,
                    effective_turn_on_soc_percent=turn_on_threshold,
                    effective_turn_off_soc_percent=turn_off_threshold,
                    forecast_liberal_factor=liberal_factor,
                        pump_reserve_soc_percent=pump_reserve,
                    solar_crossover_local=crossover_local,
                    solar_crossover_source=crossover_source,
                        reason=(
                        f"{blocked_subject} needs at least {turn_off_threshold:.1f}% SOC to keep "
                        f"running, and battery SOC is {battery_soc:.1f}%, so the pump turns off."
                    ),
                )
            return self._night_decision(
                target_on=True,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                effective_turn_on_soc_percent=turn_on_threshold,
                effective_turn_off_soc_percent=turn_off_threshold,
                forecast_liberal_factor=liberal_factor,
                pump_reserve_soc_percent=pump_reserve,
                solar_crossover_local=crossover_local,
                solar_crossover_source=crossover_source,
                reason=(
                    f"Reserve-aware night control stays on{mode_phrase} because {reference_label}'s "
                    f"sunshine forecast is {reference_sunshine:.1f} hours and battery SOC is "
                    f"{battery_soc:.1f}%, above the {turn_off_threshold:.1f}% keep-running "
                    "threshold."
                ),
            )

        if battery_soc < turn_on_threshold:
            return self._night_decision(
                target_on=False,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                effective_turn_on_soc_percent=turn_on_threshold,
                effective_turn_off_soc_percent=turn_off_threshold,
                forecast_liberal_factor=liberal_factor,
                pump_reserve_soc_percent=pump_reserve,
                solar_crossover_local=crossover_local,
                solar_crossover_source=crossover_source,
                reason=(
                    f"{blocked_subject} needs at least {turn_on_threshold:.1f}% SOC to turn on"
                    f"{reserve_clause}, and battery SOC is {battery_soc:.1f}%, so the pump stays off."
                ),
            )

        return self._night_decision(
            target_on=True,
            previous_state=previous_state,
            required_soc=required_soc,
            reference_sunshine=reference_sunshine,
            effective_turn_on_soc_percent=turn_on_threshold,
            effective_turn_off_soc_percent=turn_off_threshold,
            forecast_liberal_factor=liberal_factor,
            pump_reserve_soc_percent=pump_reserve,
            solar_crossover_local=crossover_local,
            solar_crossover_source=crossover_source,
            reason=(
                f"Reserve-aware night control can run{mode_phrase} because {reference_label}'s sunshine "
                f"forecast is {reference_sunshine:.1f} hours and battery SOC is {battery_soc:.1f}%, "
                f"meeting the {turn_on_threshold:.1f}% turn-on threshold{reserve_clause}."
            ),
        )

    def _night_decision(
        self,
        *,
        target_on: bool,
        previous_state: PumpPolicyState | None,
        required_soc: float,
        reference_sunshine: float | None,
        effective_turn_on_soc_percent: float | None,
        effective_turn_off_soc_percent: float | None,
        forecast_liberal_factor: float | None,
        reason: str,
        pump_reserve_soc_percent: float | None = None,
        solar_crossover_local: str | None = None,
        solar_crossover_source: str | None = None,
    ) -> PumpDecision:
        return PumpDecision(
            should_turn_on=target_on,
            action=PumpPolicy._action(target_on, previous_state),
            reason=reason,
            reasons=[reason],
            weather_mode="surplus_night",
            soc_control_mode="surplus_night",
            night_required_soc_percent=required_soc,
            night_reference_sunshine_hours=reference_sunshine,
            night_surplus_mode_active=True,
            night_pump_reserve_soc_percent=pump_reserve_soc_percent,
            night_solar_crossover_local=solar_crossover_local,
            night_solar_crossover_source=solar_crossover_source,
            effective_turn_on_soc_percent=effective_turn_on_soc_percent,
            effective_turn_off_soc_percent=effective_turn_off_soc_percent,
            forecast_liberal_factor=forecast_liberal_factor,
        )

    def _night_reference_sunshine(
        self,
        *,
        weather: WeatherSnapshot,
        local_now: datetime,
    ) -> tuple[str, float | None]:
        current_minutes = (local_now.hour * 60) + local_now.minute
        if current_minutes >= self._auto_off_start_minutes:
            return "tomorrow", weather.tomorrow_sunshine_hours
        return "today", weather.today_sunshine_hours

    def _night_floor_soc_percent(self) -> float:
        """The SOC the battery should still hold when solar takes over.

        The generator auto-starts on sagging DC voltage, so its effective trip point rises
        with load — observed between 20% at rest and 28% under a running compressor. The
        margin keeps the planned landing point clear of that band.
        """
        return (
            self._battery_hard_min_soc_percent
            + self._surplus_night_generator_margin_soc_percent
        )

    def _solar_crossover_minutes(
        self, *, weather: WeatherSnapshot | None, local_now: datetime
    ) -> tuple[int, str]:
        """Minutes past local midnight when solar is expected to carry the house.

        Derived from the forecast sunrise rather than a fixed clock time: sunrise moves by
        about two hours across the year here, so any hardcoded crossover is wrong for most
        of it. Crossover trails sunrise because the panels must first out-produce the house
        — measured at +58 to +65 min on clear September mornings. Falls back to the fixed
        `SURPLUS_NIGHT_SOLAR_CROSSOVER_LOCAL` when no sunrise is available.
        """
        if weather is not None:
            current_minutes = (local_now.hour * 60) + local_now.minute
            sunrise = weather.sunrise_minutes_for(
                tomorrow=current_minutes >= self._auto_off_start_minutes
            )
            if sunrise is not None:
                offset = round(self._crossover_after_sunrise_minutes)
                return (sunrise + offset) % (24 * 60), "sunrise"
        return self._solar_crossover_fallback_minutes, "fallback"

    def _required_night_soc_percent(
        self, *, local_now: datetime, weather: WeatherSnapshot | None = None
    ) -> float:
        """SOC needed now for base load alone to land on the night floor at solar crossover.

        Measured against `AUTO_RESUME_START_LOCAL` this used to fall ~1.25 h short: the
        controller resumes daytime control before solar actually carries the house, and the
        uncovered stretch sits on the night's heaviest base load.
        """
        return (
            self._night_floor_soc_percent()
            + (
                self._base_load_kwh_until_crossover(local_now=local_now, weather=weather)
                / self._battery_capacity_kwh
            )
            * 100.0
        )

    def _base_load_kwh_until_crossover(
        self, *, local_now: datetime, weather: WeatherSnapshot | None = None
    ) -> float:
        """Integrate the two-segment base-load profile from now to solar crossover.

        Base load is not flat overnight: it sits near `SURPLUS_NIGHT_BASE_LOAD_KW` through
        the small hours and steps up to `SURPLUS_NIGHT_MORNING_BASE_LOAD_KW` once the house
        wakes, which is precisely the stretch a resume-time budget omits.
        """
        crossover_at = self._next_crossover_at(local_now=local_now, weather=weather)
        if crossover_at is None:
            return 0.0
        morning_at = _previous_local_time(crossover_at, self._morning_start_minutes)
        if morning_at > crossover_at:
            morning_at = crossover_at
        if local_now >= morning_at:
            return _hours_between(local_now, crossover_at) * self._surplus_night_morning_base_load_kw
        return (
            _hours_between(local_now, morning_at) * self._surplus_night_base_load_kw
            + _hours_between(morning_at, crossover_at) * self._surplus_night_morning_base_load_kw
        )

    def _pump_start_reserve_soc_percent(
        self, *, local_now: datetime, weather: WeatherSnapshot | None = None
    ) -> float:
        """SOC a *start* must cover on top of the base-load reserve.

        Switching the plug triggers a compressor that cannot be cancelled once it fires, so a
        start commits roughly `SURPLUS_NIGHT_MIN_RUN_HOURS` of draw whatever the SOC does next.
        Charging that to the turn-on threshold — and never to the keep-running threshold —
        makes starting dearer than continuing, so the pump is not cleared to fire shortly
        before dawn. Capped at the time actually left, so it cannot overstate the commitment.
        """
        hours = min(
            self._surplus_night_min_run_hours,
            self._hours_until_crossover(local_now=local_now, weather=weather),
        )
        return ((hours * self._surplus_night_pump_load_kw) / self._battery_capacity_kwh) * 100.0

    def _hours_until_crossover(
        self, *, local_now: datetime, weather: WeatherSnapshot | None = None
    ) -> float:
        crossover_at = self._next_crossover_at(local_now=local_now, weather=weather)
        if crossover_at is None:
            return 0.0
        return _hours_between(local_now, crossover_at)

    def _next_crossover_at(
        self, *, local_now: datetime, weather: WeatherSnapshot | None = None
    ) -> datetime | None:
        if not self._is_within_quiet_hours(local_now=local_now, weather=weather):
            return None
        minutes, _ = self._solar_crossover_minutes(weather=weather, local_now=local_now)
        return _next_local_time(local_now, minutes)

    def _build_payload(
        self,
        *,
        decision: PumpDecision,
        power: dict[str, object],
        weather: dict[str, object],
        previous_state: PumpPolicyState | None,
        next_state: PumpPolicyState,
        intended_is_on: bool,
        quiet_hours_blocked: bool,
        power_status: TelemetryStatus,
        weather_source: str,
    ) -> dict[str, object]:
        return {
            "power": power,
            "power_status": power_status.to_dict(),
            "weather": weather,
            "weather_source": weather_source,
            "previous_state": previous_state.to_dict() if previous_state else None,
            "next_state": next_state.to_dict(),
            "decision": decision.to_dict(),
            "intended_target_is_on": intended_is_on,
            "quiet_hours_blocked": quiet_hours_blocked,
            "blocked_reason": QUIET_HOURS_BLOCK_REASON if quiet_hours_blocked else None,
            "night_required_soc_percent": decision.night_required_soc_percent,
            "night_reference_sunshine_hours": decision.night_reference_sunshine_hours,
            "night_surplus_mode_active": decision.night_surplus_mode_active,
            "night_pump_reserve_soc_percent": decision.night_pump_reserve_soc_percent,
            "daytime_surplus_override_active": decision.daytime_surplus_override_active,
            "daytime_projected_surplus_kw": decision.daytime_projected_surplus_kw,
            "generator_start_blocked": decision.generator_start_blocked,
            "night_solar_crossover_local": decision.night_solar_crossover_local,
            "night_solar_crossover_source": decision.night_solar_crossover_source,
            "effective_turn_on_soc_percent": decision.effective_turn_on_soc_percent,
            "effective_turn_off_soc_percent": decision.effective_turn_off_soc_percent,
            "forecast_liberal_factor": decision.forecast_liberal_factor,
            "soc_control_mode": decision.soc_control_mode,
        }

    @staticmethod
    def _observed_plug_state(actuation: PumpActuationResult) -> bool | None:
        """The plug's real state this cycle; None only when it could not be read."""
        if actuation.observed_after_is_on is not None:
            return actuation.observed_after_is_on
        return actuation.observed_before_is_on

    def _policy_fingerprint(self) -> dict[str, object]:
        return {
            "battery_capacity_kwh": self._battery_capacity_kwh,
            "night_base_load_kw": self._surplus_night_base_load_kw,
            "night_pump_load_kw": self._surplus_night_pump_load_kw,
            "battery_hard_min_soc_percent": self._battery_hard_min_soc_percent,
        }

    def _previous_intended_target_is_on(self, state: PumpPolicyState | None) -> bool | None:
        if state is None:
            return None
        return state.is_on and not state.quiet_hours_forced_off

    def _intended_target_is_on(
        self,
        automatic_target_is_on: bool,
        quiet_hours_active: bool,
    ) -> bool:
        if quiet_hours_active:
            return False
        return automatic_target_is_on

    def _resume_minutes(
        self, *, weather: WeatherSnapshot | None, local_now: datetime
    ) -> int:
        """When daytime control takes back over, in minutes past local midnight.

        Night control has to hold until solar actually carries the house, not until a fixed
        clock time: resuming at `AUTO_RESUME_START_LOCAL` handed the last stretch of darkness
        to the daytime thresholds, which could clear the pump to start on a battery whose
        remaining charge was reserved to reach crossover. Falls back to the configured time
        when the crossover cannot be derived from a sunrise.
        """
        if not self._auto_resume_follows_crossover:
            return self._auto_resume_start_minutes
        minutes, source = self._solar_crossover_minutes(weather=weather, local_now=local_now)
        return minutes if source == "sunrise" else self._auto_resume_start_minutes

    def _is_within_quiet_hours(
        self,
        *,
        local_now: datetime | None = None,
        weather: WeatherSnapshot | None = None,
    ) -> bool:
        candidate = local_now or self._local_now()
        off_start = self._auto_off_start_minutes
        resume_start = self._resume_minutes(weather=weather, local_now=candidate)
        if off_start == resume_start:
            return False

        current_minutes = (candidate.hour * 60) + candidate.minute
        if off_start < resume_start:
            return off_start <= current_minutes < resume_start
        return current_minutes >= off_start or current_minutes < resume_start

    def _local_now(self) -> datetime:
        now = self._now_provider()
        if now.tzinfo is None:
            raise ValueError("now_provider must return a timezone-aware datetime.")
        return now.astimezone(self._auto_control_timezone)

    def _weather_local_date(self) -> date:
        now = self._now_provider()
        if now.tzinfo is None:
            raise ValueError("now_provider must return a timezone-aware datetime.")
        return now.astimezone(self._weather_timezone).date()

    def _load_same_day_weather_cache(self, local_date: date) -> WeatherSnapshot | None:
        state = self._state_store.load()
        if state is None:
            return None
        return state.cached_weather_for_local_date(local_date=local_date)

    def _with_weather_cache(
        self,
        state: PumpPolicyState,
        *,
        weather: WeatherSnapshot,
        weather_source: str,
    ) -> PumpPolicyState:
        if weather_source != "live":
            return state
        return replace(
            state,
            weather_cache_local_date=self._weather_local_date().isoformat(),
            weather_cache_current_temperature_c=weather.current_temperature_c,
            weather_cache_today_min_temperature_c=weather.today_min_temperature_c,
            weather_cache_today_max_temperature_c=weather.today_max_temperature_c,
            weather_cache_today_sunshine_hours=weather.today_sunshine_hours,
            weather_cache_tomorrow_sunshine_hours=weather.tomorrow_sunshine_hours,
            weather_cache_today_sunrise_iso=weather.today_sunrise_iso,
            weather_cache_tomorrow_sunrise_iso=weather.tomorrow_sunrise_iso,
            weather_cache_today_sunset_iso=weather.today_sunset_iso,
            weather_cache_tomorrow_sunset_iso=weather.tomorrow_sunset_iso,
            weather_cache_weather_code=weather.weather_code,
            weather_cache_queried_timezone=weather.queried_timezone,
            weather_cache_cached_at_iso=datetime.now(UTC).isoformat(),
        )


def _contains_percent(percents: tuple[float, ...], candidate: float) -> bool:
    return any(abs(percent - candidate) < 1e-6 for percent in percents)


def _hhmm_to_minutes(value: str | None) -> int:
    if value is None:
        return 0
    hour_raw, minute_raw = value.split(":", 1)
    return int(hour_raw) * 60 + int(minute_raw)


def _at_minutes(moment: datetime, minutes: int) -> datetime:
    return moment.replace(
        hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0
    )


def _next_local_time(moment: datetime, minutes: int) -> datetime:
    """The first occurrence of a wall-clock time at or after `moment`."""
    candidate = _at_minutes(moment, minutes)
    if candidate < moment:
        candidate += timedelta(days=1)
    return candidate


def _previous_local_time(moment: datetime, minutes: int) -> datetime:
    """The last occurrence of a wall-clock time at or before `moment`."""
    candidate = _at_minutes(moment, minutes)
    if candidate > moment:
        candidate -= timedelta(days=1)
    return candidate


def _hours_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600.0)
