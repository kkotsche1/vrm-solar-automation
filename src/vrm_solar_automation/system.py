from __future__ import annotations

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
from .policy import PumpDecision, PumpPolicy, PumpPolicyConfig, PumpPolicyState
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
        decision: PumpDecision,
        intended_target_is_on: bool,
        quiet_hours_blocked: bool,
        blocked_reason: str | None,
        actuation: dict[str, object],
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
            sunshine_hours_min=settings.sunshine_hours_min,
        )
        self._policy = policy or PumpPolicy(policy_config)
        self._generator_alert_threshold_watts = policy_config.generator_on_block_watts
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
            )
        else:
            self._notifier = None
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._auto_off_start_minutes = _hhmm_to_minutes(settings.auto_off_start_local)
        self._auto_resume_start_minutes = _hhmm_to_minutes(settings.auto_resume_start_local)
        self._auto_control_timezone = ZoneInfo(settings.auto_control_timezone)
        self._weather_timezone = ZoneInfo(settings.weather_timezone)
        self._surplus_night_enabled = settings.surplus_night_enabled
        self._surplus_night_base_load_kw = settings.surplus_night_base_load_kw
        self._surplus_night_hard_min_soc_percent = settings.surplus_night_hard_min_soc_percent
        self._surplus_night_buffer_soc_percent = settings.surplus_night_buffer_soc_percent
        self._surplus_night_turn_on_margin_soc_percent = (
            settings.surplus_night_turn_on_margin_soc_percent
        )
        self._surplus_night_turn_off_margin_soc_percent = (
            settings.surplus_night_turn_off_margin_soc_percent
        )
        self._surplus_night_next_day_sunshine_min = settings.surplus_night_next_day_sunshine_min
        self._battery_capacity_kwh = 50.0
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
        ) = self._evaluate_policy_with_inputs(
            power=power,
            weather=weather,
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
        ) = self._evaluate_policy_with_inputs(
            power=power,
            weather=weather,
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
        final_state = self._apply_alert_state(
            final_state,
            power=power,
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
            decision=decision,
            intended_target_is_on=bool(payload["intended_target_is_on"]),
            quiet_hours_blocked=bool(payload["quiet_hours_blocked"]),
            blocked_reason=payload["blocked_reason"],
            actuation=payload["actuation"],
        )
        return decision, payload

    def _evaluate_policy_with_inputs(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
    ) -> tuple[
        PumpDecision,
        PumpPolicyState | None,
        PumpPolicyState,
        bool,
    ]:
        previous_state = self._state_store.load()
        quiet_hours_forced_off = False
        decision = self._policy.decide(
            power=power,
            weather=weather,
            previous_state=previous_state,
        )
        local_now = self._local_now()
        if self._is_within_quiet_hours(local_now=local_now):
            if self._surplus_night_enabled:
                decision = self._decide_surplus_night(
                    power=power,
                    weather=weather,
                    previous_state=previous_state,
                    local_now=local_now,
                )
            else:
                quiet_hours_forced_off = True
        next_state = StateStore.from_decision(
            previous_state,
            decision.should_turn_on,
            quiet_hours_forced_off=quiet_hours_forced_off,
        )
        return decision, previous_state, next_state, quiet_hours_forced_off

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

        should_force_reconcile = quiet_hours_active and observed_before is True and not intended_is_on
        should_reconcile = target_changed or should_force_reconcile

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

        if command_sent is not None and self._notifier is not None:
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

    def _apply_alert_state(
        self,
        state: PumpPolicyState,
        *,
        power: PowerSnapshot,
    ) -> PumpPolicyState:
        battery_soc = power.battery_soc_percent
        generator_watts = abs(power.generator_watts or 0.0)
        crossed_thresholds: list[int] = []

        battery_alert_below_40_sent = self._should_keep_battery_alert(
            battery_soc=battery_soc,
            threshold=40,
            already_sent=state.battery_alert_below_40_sent,
            crossed_thresholds=crossed_thresholds,
        )
        battery_alert_below_35_sent = self._should_keep_battery_alert(
            battery_soc=battery_soc,
            threshold=35,
            already_sent=state.battery_alert_below_35_sent,
            crossed_thresholds=crossed_thresholds,
        )
        battery_alert_below_30_sent = self._should_keep_battery_alert(
            battery_soc=battery_soc,
            threshold=30,
            already_sent=state.battery_alert_below_30_sent,
            crossed_thresholds=crossed_thresholds,
        )

        generator_running = generator_watts >= self._generator_alert_threshold_watts
        generator_running_alert_sent = state.generator_running_alert_sent
        if generator_running:
            if not generator_running_alert_sent:
                self._send_generator_started_alert(generator_watts=generator_watts)
                generator_running_alert_sent = True
        else:
            generator_running_alert_sent = False

        if crossed_thresholds:
            self._send_battery_alert(
                battery_soc_percent=battery_soc,
                crossed_thresholds=tuple(crossed_thresholds),
            )

        return replace(
            state,
            battery_alert_below_40_sent=battery_alert_below_40_sent,
            battery_alert_below_35_sent=battery_alert_below_35_sent,
            battery_alert_below_30_sent=battery_alert_below_30_sent,
            generator_running_alert_sent=generator_running_alert_sent,
        )

    def _send_battery_alert(
        self,
        *,
        battery_soc_percent: float | None,
        crossed_thresholds: tuple[int, ...],
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

    @staticmethod
    def _should_keep_battery_alert(
        *,
        battery_soc: float | None,
        threshold: int,
        already_sent: bool,
        crossed_thresholds: list[int],
    ) -> bool:
        if battery_soc is None or battery_soc > threshold:
            return False
        if not already_sent:
            crossed_thresholds.append(threshold)
        return True

    def _decide_surplus_night(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        previous_state: PumpPolicyState | None,
        local_now: datetime,
    ) -> PumpDecision:
        battery_soc = power.battery_soc_percent
        generator_watts = abs(power.generator_watts or 0.0)
        required_soc = self._required_night_soc_percent(local_now=local_now)
        reference_label, reference_sunshine = self._night_reference_sunshine(
            weather=weather,
            local_now=local_now,
        )
        turn_on_threshold = required_soc + self._surplus_night_turn_on_margin_soc_percent
        turn_off_threshold = required_soc + self._surplus_night_turn_off_margin_soc_percent
        previous_target_is_on = previous_state.is_on if previous_state is not None else False

        if battery_soc is None:
            return self._night_decision(
                target_on=False,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                reason="Battery SOC is unavailable, so reserve-aware night control fails safe to off.",
            )

        if generator_watts >= self._generator_alert_threshold_watts:
            return self._night_decision(
                target_on=False,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                reason=(
                    f"Generator power is present at {generator_watts:.0f} W, so reserve-aware night "
                    "control keeps the pump off."
                ),
            )

        if reference_sunshine is None:
            return self._night_decision(
                target_on=False,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
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
                    reason=(
                        f"Reserve-aware night control needs at least {turn_off_threshold:.1f}% SOC to keep "
                        f"running, and battery SOC is {battery_soc:.1f}%, so the pump turns off."
                    ),
                )
            return self._night_decision(
                target_on=True,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                reason=(
                    f"Reserve-aware night control stays on because {reference_label}'s sunshine forecast is "
                    f"{reference_sunshine:.1f} hours and battery SOC is {battery_soc:.1f}%, above the "
                    f"{turn_off_threshold:.1f}% keep-running threshold."
                ),
            )

        if battery_soc < turn_on_threshold:
            return self._night_decision(
                target_on=False,
                previous_state=previous_state,
                required_soc=required_soc,
                reference_sunshine=reference_sunshine,
                reason=(
                    f"Reserve-aware night control needs at least {turn_on_threshold:.1f}% SOC to turn on, "
                    f"and battery SOC is {battery_soc:.1f}%, so the pump stays off."
                ),
            )

        return self._night_decision(
            target_on=True,
            previous_state=previous_state,
            required_soc=required_soc,
            reference_sunshine=reference_sunshine,
            reason=(
                f"Reserve-aware night control can run because {reference_label}'s sunshine forecast is "
                f"{reference_sunshine:.1f} hours and battery SOC is {battery_soc:.1f}%, meeting the "
                f"{turn_on_threshold:.1f}% turn-on threshold."
            ),
        )

    def _night_decision(
        self,
        *,
        target_on: bool,
        previous_state: PumpPolicyState | None,
        required_soc: float,
        reference_sunshine: float | None,
        reason: str,
    ) -> PumpDecision:
        return PumpDecision(
            should_turn_on=target_on,
            action=PumpPolicy._action(target_on, previous_state),
            reason=reason,
            reasons=[reason],
            weather_mode="surplus_night",
            night_required_soc_percent=required_soc,
            night_reference_sunshine_hours=reference_sunshine,
            night_surplus_mode_active=True,
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

    def _required_night_soc_percent(self, *, local_now: datetime) -> float:
        hours_until_resume = self._hours_until_resume(local_now=local_now)
        reserve_soc = (
            self._surplus_night_hard_min_soc_percent
            + self._surplus_night_buffer_soc_percent
            + ((hours_until_resume * self._surplus_night_base_load_kw) / self._battery_capacity_kwh)
            * 100.0
        )
        return reserve_soc

    def _hours_until_resume(self, *, local_now: datetime) -> float:
        resume_today = local_now.replace(
            hour=self._auto_resume_start_minutes // 60,
            minute=self._auto_resume_start_minutes % 60,
            second=0,
            microsecond=0,
        )
        if not self._is_within_quiet_hours(local_now=local_now):
            return 0.0
        current_minutes = (local_now.hour * 60) + local_now.minute
        if self._auto_off_start_minutes < self._auto_resume_start_minutes:
            resume_at = resume_today
        elif current_minutes >= self._auto_off_start_minutes:
            resume_at = resume_today + timedelta(days=1)
        else:
            resume_at = resume_today
        return max(0.0, (resume_at - local_now).total_seconds() / 3600.0)

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

    def _is_within_quiet_hours(self, *, local_now: datetime | None = None) -> bool:
        candidate = local_now or self._local_now()
        off_start = self._auto_off_start_minutes
        resume_start = self._auto_resume_start_minutes
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
            weather_cache_weather_code=weather.weather_code,
            weather_cache_queried_timezone=weather.queried_timezone,
            weather_cache_cached_at_iso=datetime.now(UTC).isoformat(),
        )


def _hhmm_to_minutes(value: str | None) -> int:
    if value is None:
        return 0
    hour_raw, minute_raw = value.split(":", 1)
    return int(hour_raw) * 60 + int(minute_raw)
