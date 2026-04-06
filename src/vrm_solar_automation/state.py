from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .db import ControlCycleRecord, ControllerStateRecord, create_engine_for_url
from .policy import PumpDecision, PumpPolicyState


class StateStore:
    def __init__(self, database_url: str, *, session_factory: sessionmaker | None = None) -> None:
        self._engine: Engine | None = None
        if session_factory is None:
            self._engine = create_engine_for_url(database_url)
            self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
        else:
            self._session_factory = session_factory

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        self.close()
        return False

    def load(self) -> PumpPolicyState | None:
        with self._session_factory() as session:
            row = session.scalar(select(ControllerStateRecord).where(ControllerStateRecord.id == 1))
            if row is None:
                return None
            return PumpPolicyState(
                is_on=bool(row.is_on),
                changed_at_iso=row.changed_at_iso,
                quiet_hours_forced_off=bool(row.quiet_hours_forced_off),
                battery_alert_below_40_sent=bool(row.battery_alert_below_40_sent),
                battery_alert_below_35_sent=bool(row.battery_alert_below_35_sent),
                battery_alert_below_30_sent=bool(row.battery_alert_below_30_sent),
                generator_running_alert_sent=bool(row.generator_running_alert_sent),
                weather_cache_local_date=row.weather_cache_local_date,
                weather_cache_current_temperature_c=row.weather_cache_current_temperature_c,
                weather_cache_today_min_temperature_c=row.weather_cache_today_min_temperature_c,
                weather_cache_today_max_temperature_c=row.weather_cache_today_max_temperature_c,
                weather_cache_today_sunshine_hours=row.weather_cache_today_sunshine_hours,
                weather_cache_weather_code=row.weather_cache_weather_code,
                weather_cache_queried_timezone=row.weather_cache_queried_timezone,
                weather_cache_cached_at_iso=row.weather_cache_cached_at_iso,
                last_known_plug_is_on=row.last_known_plug_is_on,
                last_known_plug_at_iso=row.last_known_plug_at_iso,
                last_actuation_error=row.last_actuation_error,
                last_actuation_at_iso=row.last_actuation_at_iso,
            )

    def save(self, state: PumpPolicyState) -> None:
        now_iso = datetime.now(UTC).isoformat()
        with self._session_factory() as session:
            row = session.scalar(select(ControllerStateRecord).where(ControllerStateRecord.id == 1))
            if row is None:
                row = ControllerStateRecord(
                    id=1,
                    is_on=state.is_on,
                    changed_at_iso=state.changed_at_iso,
                    quiet_hours_forced_off=state.quiet_hours_forced_off,
                    battery_alert_below_40_sent=state.battery_alert_below_40_sent,
                    battery_alert_below_35_sent=state.battery_alert_below_35_sent,
                    battery_alert_below_30_sent=state.battery_alert_below_30_sent,
                    generator_running_alert_sent=state.generator_running_alert_sent,
                    weather_cache_local_date=state.weather_cache_local_date,
                    weather_cache_current_temperature_c=state.weather_cache_current_temperature_c,
                    weather_cache_today_min_temperature_c=state.weather_cache_today_min_temperature_c,
                    weather_cache_today_max_temperature_c=state.weather_cache_today_max_temperature_c,
                    weather_cache_today_sunshine_hours=state.weather_cache_today_sunshine_hours,
                    weather_cache_weather_code=state.weather_cache_weather_code,
                    weather_cache_queried_timezone=state.weather_cache_queried_timezone,
                    weather_cache_cached_at_iso=state.weather_cache_cached_at_iso,
                    last_known_plug_is_on=state.last_known_plug_is_on,
                    last_known_plug_at_iso=state.last_known_plug_at_iso,
                    last_actuation_error=state.last_actuation_error,
                    last_actuation_at_iso=state.last_actuation_at_iso,
                    updated_at_iso=now_iso,
                )
                session.add(row)
            else:
                row.is_on = state.is_on
                row.changed_at_iso = state.changed_at_iso
                row.quiet_hours_forced_off = state.quiet_hours_forced_off
                row.battery_alert_below_40_sent = state.battery_alert_below_40_sent
                row.battery_alert_below_35_sent = state.battery_alert_below_35_sent
                row.battery_alert_below_30_sent = state.battery_alert_below_30_sent
                row.generator_running_alert_sent = state.generator_running_alert_sent
                row.weather_cache_local_date = state.weather_cache_local_date
                row.weather_cache_current_temperature_c = state.weather_cache_current_temperature_c
                row.weather_cache_today_min_temperature_c = (
                    state.weather_cache_today_min_temperature_c
                )
                row.weather_cache_today_max_temperature_c = (
                    state.weather_cache_today_max_temperature_c
                )
                row.weather_cache_today_sunshine_hours = (
                    state.weather_cache_today_sunshine_hours
                )
                row.weather_cache_weather_code = state.weather_cache_weather_code
                row.weather_cache_queried_timezone = state.weather_cache_queried_timezone
                row.weather_cache_cached_at_iso = state.weather_cache_cached_at_iso
                row.last_known_plug_is_on = state.last_known_plug_is_on
                row.last_known_plug_at_iso = state.last_known_plug_at_iso
                row.last_actuation_error = state.last_actuation_error
                row.last_actuation_at_iso = state.last_actuation_at_iso
                row.updated_at_iso = now_iso
            session.commit()

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
    ) -> None:
        with self._session_factory() as session:
            session.add(
                ControlCycleRecord(
                    timestamp_unix_ms=timestamp_unix_ms,
                    timestamp_iso=timestamp_iso,
                    site_id=int(power.get("site_id") or 0),
                    site_name=str(power.get("site_name") or "unknown"),
                    site_identifier=str(power.get("site_identifier") or "unknown"),
                    power_queried_at_unix_ms=_optional_int(power.get("queried_at_unix_ms")),
                    power_queried_at_iso=_optional_str(power.get("queried_at_iso")),
                    battery_soc_percent=_optional_float(power.get("battery_soc_percent")),
                    solar_watts=_optional_float(power.get("solar_watts")),
                    house_watts=_optional_float(power.get("house_watts")),
                    house_l1_watts=_optional_float(power.get("house_l1_watts")),
                    house_l2_watts=_optional_float(power.get("house_l2_watts")),
                    house_l3_watts=_optional_float(power.get("house_l3_watts")),
                    generator_watts=_optional_float(power.get("generator_watts")),
                    active_input_source=_optional_int(power.get("active_input_source")),
                    current_temperature_c=_optional_float(weather.get("current_temperature_c")),
                    today_min_temperature_c=_optional_float(weather.get("today_min_temperature_c")),
                    today_max_temperature_c=_optional_float(weather.get("today_max_temperature_c")),
                    today_sunshine_hours=_optional_float(weather.get("today_sunshine_hours")),
                    weather_code=_optional_int(weather.get("weather_code")),
                    queried_timezone=_optional_str(weather.get("queried_timezone")),
                    weather_source=weather_source,
                    should_turn_on=decision.should_turn_on,
                    decision_action=decision.action,
                    decision_reason=decision.reason,
                    decision_weather_mode=decision.weather_mode,
                    intended_target_is_on=intended_target_is_on,
                    quiet_hours_blocked=quiet_hours_blocked,
                    blocked_reason=blocked_reason,
                    actuation_status=str(actuation.get("status") or "unknown"),
                    actuation_command_sent=_optional_str(actuation.get("command_sent")),
                    actuation_observed_before_is_on=_optional_bool(
                        actuation.get("observed_before_is_on")
                    ),
                    actuation_observed_after_is_on=_optional_bool(
                        actuation.get("observed_after_is_on")
                    ),
                    actuation_error=_optional_str(actuation.get("error")),
                )
            )
            session.commit()

    @staticmethod
    def from_decision(
        previous_state: PumpPolicyState | None,
        should_turn_on: bool,
        *,
        quiet_hours_forced_off: bool = False,
    ) -> PumpPolicyState:
        if (
            previous_state
            and previous_state.is_on == should_turn_on
            and previous_state.quiet_hours_forced_off == quiet_hours_forced_off
        ):
            return previous_state
        if previous_state is not None:
            return replace(
                previous_state,
                is_on=should_turn_on,
                changed_at_iso=datetime.now(UTC).isoformat(),
                quiet_hours_forced_off=quiet_hours_forced_off,
            )
        return PumpPolicyState(
            is_on=should_turn_on,
            changed_at_iso=datetime.now(UTC).isoformat(),
            quiet_hours_forced_off=quiet_hours_forced_off,
        )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)
