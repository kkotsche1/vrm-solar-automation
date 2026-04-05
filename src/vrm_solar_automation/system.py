from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
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
        self._policy = policy or PumpPolicy(
            PumpPolicyConfig(
                battery_min_soc=settings.battery_min_soc_percent,
            )
        )
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
        self._summer_start_month_day = _month_day_to_tuple(settings.summer_start_month_day)
        self._winter_start_month_day = _month_day_to_tuple(settings.winter_start_month_day)
        self._summer_auto_off_start_minutes = _hhmm_to_minutes(settings.summer_auto_off_start_local)
        self._summer_auto_resume_start_minutes = _hhmm_to_minutes(
            settings.summer_auto_resume_start_local
        )
        self._winter_auto_off_start_minutes = _hhmm_to_minutes(settings.winter_auto_off_start_local)
        self._winter_auto_resume_start_minutes = _hhmm_to_minutes(
            settings.winter_auto_resume_start_local
        )
        self._auto_control_timezone = ZoneInfo(settings.auto_control_timezone)
        self._weather_timezone = ZoneInfo(settings.weather_timezone)
        self._weather_cache_date: date | None = None
        self._weather_cache_snapshot: WeatherSnapshot | None = None

    async def evaluate(self) -> tuple[PumpDecision, dict[str, object]]:
        power, power_status = await self._fetch_power()
        weather = await self._fetch_weather()
        return await self.evaluate_with_inputs(
            power=power,
            weather=weather,
            power_status=power_status,
        )

    async def control(self) -> tuple[PumpDecision, dict[str, object]]:
        power, power_status = await self._fetch_power()
        weather = await self._fetch_weather()
        return await self.control_with_inputs(
            power=power,
            weather=weather,
            power_status=power_status,
        )

    async def evaluate_with_inputs(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        power_status: TelemetryStatus,
    ) -> tuple[PumpDecision, dict[str, object]]:
        decision, previous_state, next_state = self._evaluate_policy_with_inputs(
            power=power,
            weather=weather,
        )
        quiet_hours_active = self._is_within_quiet_hours()
        next_state = self._with_quiet_hours_state(
            next_state,
            quiet_hours_forced_off=quiet_hours_active,
        )
        intended_is_on = self._intended_target_is_on(next_state.is_on, quiet_hours_active)
        quiet_hours_blocked = quiet_hours_active and next_state.is_on

        return decision, self._build_payload(
            decision=decision,
            power=power.to_dict(),
            weather=weather.to_dict(),
            previous_state=previous_state,
            next_state=next_state,
            intended_is_on=intended_is_on,
            quiet_hours_blocked=quiet_hours_blocked,
            power_status=power_status,
        )

    async def control_with_inputs(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        power_status: TelemetryStatus,
        force_apply: bool = False,
    ) -> tuple[PumpDecision, dict[str, object]]:
        decision, previous_state, next_state = self._evaluate_policy_with_inputs(
            power=power,
            weather=weather,
        )
        quiet_hours_active = self._is_within_quiet_hours()
        next_state = self._with_quiet_hours_state(
            next_state,
            quiet_hours_forced_off=quiet_hours_active,
        )
        intended_is_on = self._intended_target_is_on(next_state.is_on, quiet_hours_active)
        quiet_hours_blocked = quiet_hours_active and next_state.is_on
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
            quiet_hours_active=quiet_hours_active,
            quiet_hours_blocked=quiet_hours_blocked,
            target_changed=target_changed,
            decision_action=decision.action,
            decision_reason=decision.reason,
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
        )
        payload["actuation"] = actuation.to_dict()
        cycle_timestamp = datetime.now(UTC)
        self._state_store.record_control_cycle(
            timestamp_unix_ms=int(cycle_timestamp.timestamp() * 1000),
            timestamp_iso=cycle_timestamp.isoformat(),
            power=payload["power"],
            weather=payload["weather"],
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
    ]:
        previous_state = self._state_store.load()
        decision = self._policy.decide(
            power=power,
            weather=weather,
            previous_state=previous_state,
        )
        next_state = StateStore.from_decision(previous_state, decision.should_turn_on)
        return decision, previous_state, next_state

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
        today = datetime.now(self._weather_timezone).date()
        if self._weather_cache_date == today and self._weather_cache_snapshot is not None:
            return self._weather_cache_snapshot

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
            self._weather_cache_date = today
            self._weather_cache_snapshot = weather
        except aiohttp.ClientError:
            pass
        return weather

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
        return PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            quiet_hours_forced_off=state.quiet_hours_forced_off,
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
        )

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
    ) -> dict[str, object]:
        return {
            "power": power,
            "power_status": power_status.to_dict(),
            "weather": weather,
            "previous_state": previous_state.to_dict() if previous_state else None,
            "next_state": next_state.to_dict(),
            "decision": decision.to_dict(),
            "intended_target_is_on": intended_is_on,
            "quiet_hours_blocked": quiet_hours_blocked,
            "blocked_reason": QUIET_HOURS_BLOCK_REASON if quiet_hours_blocked else None,
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

    def _is_within_quiet_hours(self) -> bool:
        local_now = self._local_now()
        off_start, resume_start = self._current_quiet_hours_window(local_now.date())
        if off_start == resume_start:
            return False

        current_minutes = (local_now.hour * 60) + local_now.minute
        if off_start < resume_start:
            return off_start <= current_minutes < resume_start
        return current_minutes >= off_start or current_minutes < resume_start

    def _current_quiet_hours_window(self, current_date: date) -> tuple[int, int]:
        if not self._has_seasonal_quiet_hours():
            return self._auto_off_start_minutes, self._auto_resume_start_minutes
        if self._is_summer_date(current_date):
            return self._summer_auto_off_start_minutes, self._summer_auto_resume_start_minutes
        return self._winter_auto_off_start_minutes, self._winter_auto_resume_start_minutes

    def _has_seasonal_quiet_hours(self) -> bool:
        return self._summer_start_month_day is not None and self._winter_start_month_day is not None

    def _is_summer_date(self, current_date: date) -> bool:
        current_month_day = (current_date.month, current_date.day)
        summer_start = self._summer_start_month_day
        winter_start = self._winter_start_month_day
        if summer_start is None or winter_start is None:
            return False
        if summer_start < winter_start:
            return summer_start <= current_month_day < winter_start
        return current_month_day >= summer_start or current_month_day < winter_start

    def _local_now(self) -> datetime:
        now = self._now_provider()
        if now.tzinfo is None:
            raise ValueError("now_provider must return a timezone-aware datetime.")
        return now.astimezone(self._auto_control_timezone)

    @staticmethod
    def _with_quiet_hours_state(
        state: PumpPolicyState,
        *,
        quiet_hours_forced_off: bool,
    ) -> PumpPolicyState:
        if state.quiet_hours_forced_off == quiet_hours_forced_off:
            return state
        return PumpPolicyState(
            is_on=state.is_on,
            changed_at_iso=state.changed_at_iso,
            quiet_hours_forced_off=quiet_hours_forced_off,
            last_known_plug_is_on=state.last_known_plug_is_on,
            last_known_plug_at_iso=state.last_known_plug_at_iso,
            last_actuation_error=state.last_actuation_error,
            last_actuation_at_iso=state.last_actuation_at_iso,
        )


def _hhmm_to_minutes(value: str | None) -> int:
    if value is None:
        return 0
    hour_raw, minute_raw = value.split(":", 1)
    return int(hour_raw) * 60 + int(minute_raw)


def _month_day_to_tuple(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    month_raw, day_raw = value.split("-", 1)
    return int(month_raw), int(day_raw)
