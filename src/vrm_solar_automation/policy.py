from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .models import PowerSnapshot
from .weather import WeatherSnapshot


@dataclass(frozen=True)
class PumpPolicyConfig:
    battery_min_soc: float = 55.0
    battery_soft_min_soc: float = 35.0
    battery_hard_min_soc: float = 30.0
    sunshine_hours_min: float = 6.5
    forecast_liberal_sunshine_hours_min: float = 9.0
    forecast_liberal_sunshine_hours_max: float = 12.0
    auto_resume_start_local: str = "08:00"
    day_morning_bias_end_local: str = "11:00"
    generator_on_block_watts: float = 100.0


@dataclass(frozen=True)
class PumpPolicyState:
    is_on: bool
    changed_at_iso: str
    quiet_hours_forced_off: bool = False
    consecutive_power_failures: int = 0
    last_power_failure_at_iso: str | None = None
    last_power_failure_error: str | None = None
    battery_alert_below_40_sent: bool = False
    battery_alert_below_35_sent: bool = False
    battery_alert_below_30_sent: bool = False
    generator_running_alert_sent: bool = False
    weather_block_alert_sent_local_date: str | None = None
    weather_cache_local_date: str | None = None
    weather_cache_current_temperature_c: float | None = None
    weather_cache_today_min_temperature_c: float | None = None
    weather_cache_today_max_temperature_c: float | None = None
    weather_cache_today_sunshine_hours: float | None = None
    weather_cache_tomorrow_sunshine_hours: float | None = None
    weather_cache_weather_code: int | None = None
    weather_cache_queried_timezone: str | None = None
    weather_cache_cached_at_iso: str | None = None
    last_known_plug_is_on: bool | None = None
    last_known_plug_at_iso: str | None = None
    last_actuation_error: str | None = None
    last_actuation_at_iso: str | None = None

    @property
    def changed_at(self) -> datetime:
        return datetime.fromisoformat(self.changed_at_iso)

    def cached_weather_for_local_date(self, *, local_date: date) -> WeatherSnapshot | None:
        if self.weather_cache_local_date != local_date.isoformat():
            return None
        if self.weather_cache_queried_timezone is None:
            return None
        return WeatherSnapshot(
            current_temperature_c=self.weather_cache_current_temperature_c,
            today_min_temperature_c=self.weather_cache_today_min_temperature_c,
            today_max_temperature_c=self.weather_cache_today_max_temperature_c,
            today_sunshine_hours=self.weather_cache_today_sunshine_hours,
            weather_code=self.weather_cache_weather_code,
            queried_timezone=self.weather_cache_queried_timezone,
            tomorrow_sunshine_hours=self.weather_cache_tomorrow_sunshine_hours,
        )

    def to_dict(self) -> dict[str, str | bool | float | int | None]:
        return {
            "is_on": self.is_on,
            "changed_at_iso": self.changed_at_iso,
            "quiet_hours_forced_off": self.quiet_hours_forced_off,
            "consecutive_power_failures": self.consecutive_power_failures,
            "last_power_failure_at_iso": self.last_power_failure_at_iso,
            "last_power_failure_error": self.last_power_failure_error,
            "battery_alert_below_40_sent": self.battery_alert_below_40_sent,
            "battery_alert_below_35_sent": self.battery_alert_below_35_sent,
            "battery_alert_below_30_sent": self.battery_alert_below_30_sent,
            "generator_running_alert_sent": self.generator_running_alert_sent,
            "weather_block_alert_sent_local_date": self.weather_block_alert_sent_local_date,
            "weather_cache_local_date": self.weather_cache_local_date,
            "weather_cache_current_temperature_c": self.weather_cache_current_temperature_c,
            "weather_cache_today_min_temperature_c": self.weather_cache_today_min_temperature_c,
            "weather_cache_today_max_temperature_c": self.weather_cache_today_max_temperature_c,
            "weather_cache_today_sunshine_hours": self.weather_cache_today_sunshine_hours,
            "weather_cache_tomorrow_sunshine_hours": self.weather_cache_tomorrow_sunshine_hours,
            "weather_cache_weather_code": self.weather_cache_weather_code,
            "weather_cache_queried_timezone": self.weather_cache_queried_timezone,
            "weather_cache_cached_at_iso": self.weather_cache_cached_at_iso,
            "last_known_plug_is_on": self.last_known_plug_is_on,
            "last_known_plug_at_iso": self.last_known_plug_at_iso,
            "last_actuation_error": self.last_actuation_error,
            "last_actuation_at_iso": self.last_actuation_at_iso,
        }


@dataclass(frozen=True)
class PumpDecision:
    should_turn_on: bool
    action: str
    reason: str
    reasons: list[str] = field(default_factory=list)
    weather_mode: str = "unknown"
    soc_control_mode: str = "daytime_adaptive"
    night_required_soc_percent: float | None = None
    night_reference_sunshine_hours: float | None = None
    night_surplus_mode_active: bool = False
    effective_turn_on_soc_percent: float | None = None
    effective_turn_off_soc_percent: float | None = None
    forecast_liberal_factor: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "should_turn_on": self.should_turn_on,
            "action": self.action,
            "reason": self.reason,
            "reasons": self.reasons,
            "weather_mode": self.weather_mode,
            "soc_control_mode": self.soc_control_mode,
            "night_required_soc_percent": self.night_required_soc_percent,
            "night_reference_sunshine_hours": self.night_reference_sunshine_hours,
            "night_surplus_mode_active": self.night_surplus_mode_active,
            "effective_turn_on_soc_percent": self.effective_turn_on_soc_percent,
            "effective_turn_off_soc_percent": self.effective_turn_off_soc_percent,
            "forecast_liberal_factor": self.forecast_liberal_factor,
        }


class PumpPolicy:
    def __init__(self, config: PumpPolicyConfig | None = None) -> None:
        self._config = config or PumpPolicyConfig()
        self._auto_resume_start_minutes = _hhmm_to_minutes(self._config.auto_resume_start_local)
        self._day_morning_bias_end_minutes = _hhmm_to_minutes(
            self._config.day_morning_bias_end_local
        )

    def decide(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        previous_state: PumpPolicyState | None,
        now: datetime | None = None,
    ) -> PumpDecision:
        weather_mode = self._classify_weather(weather)
        battery_soc = power.battery_soc_percent
        generator_watts = abs(power.generator_watts or 0.0)
        sunshine_hours = weather.today_sunshine_hours
        daytime_soc_sunshine_hours = self._daytime_soc_sunshine_hours(weather)
        tomorrow_reserve_suffix = self._daytime_soc_reason_suffix(weather=weather)
        previous_target_is_on = previous_state.is_on if previous_state is not None else False
        liberal_factor = forecast_liberal_factor(
            daytime_soc_sunshine_hours,
            liberal_sunshine_hours_min=self._config.forecast_liberal_sunshine_hours_min,
            liberal_sunshine_hours_max=self._config.forecast_liberal_sunshine_hours_max,
        )
        thresholds = self._daytime_thresholds(
            liberal_factor=liberal_factor,
            keep_running_bias_active=(
                previous_target_is_on and self._is_morning_bias_active(local_now=now)
            ),
        )
        effective_turn_on_soc_percent = (
            thresholds.turn_on_soc if daytime_soc_sunshine_hours is not None else None
        )
        effective_turn_off_soc_percent = (
            thresholds.turn_off_soc if daytime_soc_sunshine_hours is not None else None
        )

        if battery_soc is None:
            return PumpDecision(
                should_turn_on=False,
                action=self._action(False, previous_state),
                reason="Battery SOC is unavailable, so the policy fails safe to off.",
                reasons=["Battery SOC is unavailable."],
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                forecast_liberal_factor=liberal_factor,
            )

        if generator_watts >= self._config.generator_on_block_watts:
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                forecast_liberal_factor=liberal_factor,
                reason=(
                    f"Generator power is present at {generator_watts:.0f} W, so the pump should stay off."
                ),
            )

        if weather_mode == "unknown":
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=None,
                effective_turn_off_soc_percent=None,
                forecast_liberal_factor=None,
                reason="Today's sunshine-hours forecast is unavailable, so automatic control stays off.",
            )

        if weather_mode == "insufficient_sun":
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                forecast_liberal_factor=liberal_factor,
                reason=(
                    f"Today's sunshine forecast is {sunshine_hours:.1f} hours, below the "
                    f"{self._config.sunshine_hours_min:.1f}-hour minimum, so automatic demand is off."
                ),
            )

        if battery_soc <= self._config.battery_hard_min_soc:
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                forecast_liberal_factor=liberal_factor,
                reason=(
                    f"Today's sunshine forecast is {sunshine_hours:.1f} hours, but battery SOC is "
                    f"{battery_soc:.1f}%, at or below the {self._config.battery_hard_min_soc:.1f}% hard automatic "
                    "cutoff, so the pump should stay off."
                ),
            )

        if previous_target_is_on:
            if battery_soc <= thresholds.turn_off_soc:
                return self._decision(
                    target_on=False,
                    previous_state=previous_state,
                    weather_mode=weather_mode,
                    effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                    effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                    forecast_liberal_factor=liberal_factor,
                    reason=(
                        f"Today's sunshine forecast is {sunshine_hours:.1f} hours, but adaptive daytime control "
                        f"needs at least {thresholds.turn_off_soc:.1f}% SOC to keep running and battery SOC is "
                        f"{battery_soc:.1f}%, so the pump turns off{tomorrow_reserve_suffix}."
                    ),
                )
            return self._decision(
                target_on=True,
                previous_state=previous_state,
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                forecast_liberal_factor=liberal_factor,
                reason=(
                    f"Today's sunshine forecast is {sunshine_hours:.1f} hours, meeting the "
                    f"{self._config.sunshine_hours_min:.1f}-hour minimum, and battery SOC is {battery_soc:.1f}%, "
                    f"above the adaptive {thresholds.turn_off_soc:.1f}% keep-running threshold"
                    f"{tomorrow_reserve_suffix}."
                ),
            )

        if battery_soc < thresholds.turn_on_soc:
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                forecast_liberal_factor=liberal_factor,
                reason=(
                    f"Today's sunshine forecast is {sunshine_hours:.1f} hours, but adaptive daytime control "
                    f"needs at least {thresholds.turn_on_soc:.1f}% SOC to turn on and battery SOC is "
                    f"{battery_soc:.1f}%, so the pump stays off{tomorrow_reserve_suffix}."
                ),
            )

        return self._decision(
            target_on=True,
            previous_state=previous_state,
            weather_mode=weather_mode,
            effective_turn_on_soc_percent=effective_turn_on_soc_percent,
            effective_turn_off_soc_percent=effective_turn_off_soc_percent,
            forecast_liberal_factor=liberal_factor,
            reason=(
                f"Today's sunshine forecast is {sunshine_hours:.1f} hours, meeting the "
                f"{self._config.sunshine_hours_min:.1f}-hour minimum, and battery SOC is {battery_soc:.1f}%, "
                f"meeting the adaptive {thresholds.turn_on_soc:.1f}% turn-on threshold"
                f"{tomorrow_reserve_suffix}."
            ),
        )

    def _classify_weather(self, weather: WeatherSnapshot) -> str:
        sunshine_hours = weather.today_sunshine_hours
        if sunshine_hours is None:
            return "unknown"
        if sunshine_hours < self._config.sunshine_hours_min:
            return "insufficient_sun"
        return "sufficient_sun"

    @staticmethod
    def _daytime_soc_sunshine_hours(weather: WeatherSnapshot) -> float | None:
        if weather.today_sunshine_hours is None:
            return None
        if weather.tomorrow_sunshine_hours is None:
            return weather.today_sunshine_hours
        return min(weather.today_sunshine_hours, weather.tomorrow_sunshine_hours)

    @staticmethod
    def _daytime_soc_reason_suffix(*, weather: WeatherSnapshot) -> str:
        today_sunshine_hours = weather.today_sunshine_hours
        tomorrow_sunshine_hours = weather.tomorrow_sunshine_hours
        if (
            today_sunshine_hours is None
            or tomorrow_sunshine_hours is None
            or tomorrow_sunshine_hours >= today_sunshine_hours
        ):
            return ""
        return (
            f" because tomorrow's weaker {tomorrow_sunshine_hours:.1f}-hour sunshine forecast "
            "keeps daytime SOC thresholds conservative"
        )

    def _decision(
        self,
        *,
        target_on: bool,
        previous_state: PumpPolicyState | None,
        weather_mode: str,
        reason: str,
        effective_turn_on_soc_percent: float | None = None,
        effective_turn_off_soc_percent: float | None = None,
        forecast_liberal_factor: float | None = None,
    ) -> PumpDecision:
        return PumpDecision(
            should_turn_on=target_on,
            action=self._action(target_on, previous_state),
            reason=reason,
            reasons=[reason],
            weather_mode=weather_mode,
            effective_turn_on_soc_percent=effective_turn_on_soc_percent,
            effective_turn_off_soc_percent=effective_turn_off_soc_percent,
            forecast_liberal_factor=forecast_liberal_factor,
        )

    def _daytime_thresholds(
        self,
        *,
        liberal_factor: float | None,
        keep_running_bias_active: bool,
    ) -> "_DaytimeThresholds":
        factor = liberal_factor or 0.0
        turn_on_soc = linear_interpolate(
            self._config.battery_min_soc,
            self._config.battery_soft_min_soc + 5.0,
            factor,
        )
        turn_off_soc = linear_interpolate(
            self._config.battery_min_soc,
            self._config.battery_soft_min_soc,
            factor,
        )
        if keep_running_bias_active:
            turn_off_soc = max(
                self._config.battery_hard_min_soc,
                turn_off_soc - (5.0 * factor),
            )
        return _DaytimeThresholds(
            turn_on_soc=turn_on_soc,
            turn_off_soc=turn_off_soc,
        )

    def _is_morning_bias_active(self, *, local_now: datetime | None) -> bool:
        if local_now is None:
            return False
        current_minutes = (local_now.hour * 60) + local_now.minute
        return self._auto_resume_start_minutes <= current_minutes <= self._day_morning_bias_end_minutes

    @staticmethod
    def _action(target_on: bool, previous_state: PumpPolicyState | None) -> str:
        if previous_state is None:
            return "turn_on" if target_on else "turn_off"
        if previous_state.is_on == target_on:
            return "keep_on" if target_on else "keep_off"
        return "turn_on" if target_on else "turn_off"


@dataclass(frozen=True)
class _DaytimeThresholds:
    turn_on_soc: float
    turn_off_soc: float


def forecast_liberal_factor(
    sunshine_hours: float | None,
    *,
    liberal_sunshine_hours_min: float,
    liberal_sunshine_hours_max: float,
) -> float | None:
    if sunshine_hours is None:
        return None
    if liberal_sunshine_hours_max <= liberal_sunshine_hours_min:
        return 1.0 if sunshine_hours >= liberal_sunshine_hours_max else 0.0
    if sunshine_hours <= liberal_sunshine_hours_min:
        return 0.0
    if sunshine_hours >= liberal_sunshine_hours_max:
        return 1.0
    return (sunshine_hours - liberal_sunshine_hours_min) / (
        liberal_sunshine_hours_max - liberal_sunshine_hours_min
    )


def linear_interpolate(start: float, end: float, factor: float) -> float:
    clamped_factor = min(1.0, max(0.0, factor))
    return start + ((end - start) * clamped_factor)


def _hhmm_to_minutes(value: str) -> int:
    hour_raw, minute_raw = value.split(":", 1)
    return (int(hour_raw) * 60) + int(minute_raw)
