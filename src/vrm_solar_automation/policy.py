from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .models import PowerSnapshot
from .weather import WeatherSnapshot


@dataclass(frozen=True)
class PumpPolicyConfig:
    battery_min_soc: float = 55.0
    battery_soft_min_soc: float = 35.0
    battery_hard_min_soc: float = 22.5
    sunshine_hours_min: float = 6.5
    forecast_liberal_sunshine_hours_min: float = 9.0
    forecast_liberal_sunshine_hours_max: float = 12.0
    generator_alert_watts: float = 100.0
    battery_alert_soc_percents: tuple[float, ...] = (35.0, 25.0)
    battery_alert_rearm_margin_percent: float = 5.0
    daytime_surplus_turn_on_enabled: bool = True
    daytime_surplus_pump_load_kw: float = 2.1
    daytime_surplus_margin_kw: float = 0.5
    daytime_surplus_floor_soc: float = 27.5


@dataclass(frozen=True)
class PumpPolicyState:
    is_on: bool
    changed_at_iso: str
    quiet_hours_forced_off: bool = False
    consecutive_power_failures: int = 0
    last_power_failure_at_iso: str | None = None
    last_power_failure_error: str | None = None
    battery_alert_latched_percents: tuple[float, ...] = ()
    generator_running_alert_sent: bool = False
    weather_block_alert_sent_local_date: str | None = None
    plug_mismatch_alert_sent: bool = False
    weather_cache_local_date: str | None = None
    weather_cache_current_temperature_c: float | None = None
    weather_cache_today_min_temperature_c: float | None = None
    weather_cache_today_max_temperature_c: float | None = None
    weather_cache_today_sunshine_hours: float | None = None
    weather_cache_tomorrow_sunshine_hours: float | None = None
    weather_cache_today_sunrise_iso: str | None = None
    weather_cache_tomorrow_sunrise_iso: str | None = None
    weather_cache_today_sunset_iso: str | None = None
    weather_cache_tomorrow_sunset_iso: str | None = None
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
            today_sunrise_iso=self.weather_cache_today_sunrise_iso,
            tomorrow_sunrise_iso=self.weather_cache_tomorrow_sunrise_iso,
            today_sunset_iso=self.weather_cache_today_sunset_iso,
            tomorrow_sunset_iso=self.weather_cache_tomorrow_sunset_iso,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "is_on": self.is_on,
            "changed_at_iso": self.changed_at_iso,
            "quiet_hours_forced_off": self.quiet_hours_forced_off,
            "consecutive_power_failures": self.consecutive_power_failures,
            "last_power_failure_at_iso": self.last_power_failure_at_iso,
            "last_power_failure_error": self.last_power_failure_error,
            "battery_alert_latched_percents": list(self.battery_alert_latched_percents),
            "generator_running_alert_sent": self.generator_running_alert_sent,
            "weather_block_alert_sent_local_date": self.weather_block_alert_sent_local_date,
            "plug_mismatch_alert_sent": self.plug_mismatch_alert_sent,
            "weather_cache_local_date": self.weather_cache_local_date,
            "weather_cache_current_temperature_c": self.weather_cache_current_temperature_c,
            "weather_cache_today_min_temperature_c": self.weather_cache_today_min_temperature_c,
            "weather_cache_today_max_temperature_c": self.weather_cache_today_max_temperature_c,
            "weather_cache_today_sunshine_hours": self.weather_cache_today_sunshine_hours,
            "weather_cache_tomorrow_sunshine_hours": self.weather_cache_tomorrow_sunshine_hours,
            "weather_cache_today_sunrise_iso": self.weather_cache_today_sunrise_iso,
            "weather_cache_tomorrow_sunrise_iso": self.weather_cache_tomorrow_sunrise_iso,
            "weather_cache_today_sunset_iso": self.weather_cache_today_sunset_iso,
            "weather_cache_tomorrow_sunset_iso": self.weather_cache_tomorrow_sunset_iso,
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
    night_pump_reserve_soc_percent: float | None = None
    night_solar_crossover_local: str | None = None
    night_solar_crossover_source: str | None = None
    effective_turn_on_soc_percent: float | None = None
    effective_turn_off_soc_percent: float | None = None
    forecast_liberal_factor: float | None = None
    daytime_surplus_override_active: bool = False
    daytime_projected_surplus_kw: float | None = None
    generator_start_blocked: bool = False

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
            "night_pump_reserve_soc_percent": self.night_pump_reserve_soc_percent,
            "night_solar_crossover_local": self.night_solar_crossover_local,
            "night_solar_crossover_source": self.night_solar_crossover_source,
            "effective_turn_on_soc_percent": self.effective_turn_on_soc_percent,
            "effective_turn_off_soc_percent": self.effective_turn_off_soc_percent,
            "forecast_liberal_factor": self.forecast_liberal_factor,
            "daytime_surplus_override_active": self.daytime_surplus_override_active,
            "daytime_projected_surplus_kw": self.daytime_projected_surplus_kw,
            "generator_start_blocked": self.generator_start_blocked,
        }


class PumpPolicy:
    def __init__(self, config: PumpPolicyConfig | None = None) -> None:
        self._config = config or PumpPolicyConfig()

    def decide(
        self,
        *,
        power: PowerSnapshot,
        weather: WeatherSnapshot,
        previous_state: PumpPolicyState | None,
    ) -> PumpDecision:
        weather_mode = self._classify_weather(weather)
        battery_soc = power.battery_soc_percent
        sunshine_hours = weather.today_sunshine_hours
        daytime_soc_sunshine_hours = self._daytime_soc_sunshine_hours(weather)
        tomorrow_reserve_suffix = self._daytime_soc_reason_suffix(weather=weather)
        previous_target_is_on = previous_state.is_on if previous_state is not None else False
        projected_surplus_kw = self._projected_surplus_kw(
            power=power, pump_is_on=previous_target_is_on
        )
        liberal_factor = forecast_liberal_factor(
            daytime_soc_sunshine_hours,
            liberal_sunshine_hours_min=self._config.forecast_liberal_sunshine_hours_min,
            liberal_sunshine_hours_max=self._config.forecast_liberal_sunshine_hours_max,
        )
        thresholds = self._daytime_thresholds(liberal_factor=liberal_factor)
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

        if weather_mode == "unknown":
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=None,
                effective_turn_off_soc_percent=None,
                forecast_liberal_factor=None,
                daytime_projected_surplus_kw=projected_surplus_kw,
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
                daytime_projected_surplus_kw=projected_surplus_kw,
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
                daytime_projected_surplus_kw=projected_surplus_kw,
                reason=(
                    f"Today's sunshine forecast is {sunshine_hours:.1f} hours, but battery SOC is "
                    f"{battery_soc:.1f}%, at or below the {self._config.battery_hard_min_soc:.1f}% hard automatic "
                    "cutoff, so the pump should stay off."
                ),
            )

        if previous_target_is_on:
            if battery_soc <= thresholds.turn_off_soc:
                if self._surplus_override_active(
                    projected_surplus_kw=projected_surplus_kw, battery_soc=battery_soc
                ):
                    return self._decision(
                        target_on=True,
                        previous_state=previous_state,
                        weather_mode=weather_mode,
                        effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                        effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                        forecast_liberal_factor=liberal_factor,
                        daytime_projected_surplus_kw=projected_surplus_kw,
                        daytime_surplus_override_active=True,
                        reason=(
                            f"Battery SOC is {battery_soc:.1f}%, below the adaptive "
                            f"{thresholds.turn_off_soc:.1f}% keep-running threshold, but solar is "
                            f"carrying the pump with {projected_surplus_kw:.2f} kW to spare, so the "
                            "battery is not being drawn down and the pump keeps running."
                        ),
                    )
                return self._decision(
                    target_on=False,
                    previous_state=previous_state,
                    weather_mode=weather_mode,
                    effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                    effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                    forecast_liberal_factor=liberal_factor,
                    daytime_projected_surplus_kw=projected_surplus_kw,
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
                daytime_projected_surplus_kw=projected_surplus_kw,
                reason=(
                    f"Today's sunshine forecast is {sunshine_hours:.1f} hours, meeting the "
                    f"{self._config.sunshine_hours_min:.1f}-hour minimum, and battery SOC is {battery_soc:.1f}%, "
                    f"above the adaptive {thresholds.turn_off_soc:.1f}% keep-running threshold"
                    f"{tomorrow_reserve_suffix}."
                ),
            )

        if battery_soc < thresholds.turn_on_soc:
            if self._surplus_override_active(
                projected_surplus_kw=projected_surplus_kw, battery_soc=battery_soc
            ):
                return self._decision(
                    target_on=True,
                    previous_state=previous_state,
                    weather_mode=weather_mode,
                    effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                    effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                    forecast_liberal_factor=liberal_factor,
                    daytime_projected_surplus_kw=projected_surplus_kw,
                    daytime_surplus_override_active=True,
                    reason=(
                        f"Battery SOC is {battery_soc:.1f}%, below the adaptive "
                        f"{thresholds.turn_on_soc:.1f}% turn-on threshold, but live solar covers the "
                        f"pump with {projected_surplus_kw:.2f} kW to spare, so running it charges the "
                        "battery instead of draining it and the pump turns on."
                    ),
                )
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                effective_turn_on_soc_percent=effective_turn_on_soc_percent,
                effective_turn_off_soc_percent=effective_turn_off_soc_percent,
                forecast_liberal_factor=liberal_factor,
                daytime_projected_surplus_kw=projected_surplus_kw,
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
            daytime_projected_surplus_kw=projected_surplus_kw,
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
        daytime_surplus_override_active: bool = False,
        daytime_projected_surplus_kw: float | None = None,
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
            daytime_surplus_override_active=daytime_surplus_override_active,
            daytime_projected_surplus_kw=daytime_projected_surplus_kw,
        )

    def _projected_surplus_kw(
        self, *, power: PowerSnapshot, pump_is_on: bool
    ) -> float | None:
        """Solar headroom that would remain with the pump running, in kW.

        `house_watts` already contains the pump's draw whenever it is running, so only an
        off pump has its load subtracted. A positive result means running the pump charges
        the battery rather than discharging it.
        """
        if power.solar_watts is None or power.house_watts is None:
            return None
        surplus_kw = (power.solar_watts - power.house_watts) / 1000.0
        if not pump_is_on:
            surplus_kw -= self._config.daytime_surplus_pump_load_kw
        return surplus_kw

    def _surplus_override_active(
        self, *, projected_surplus_kw: float | None, battery_soc: float
    ) -> bool:
        """Whether live solar alone justifies running the pump below the SOC thresholds.

        The adaptive SOC thresholds exist to keep the battery clear of the generator's
        auto-start band, but they are blind to production: on a bright morning the night
        reserve lands the battery below the turn-on threshold by design, locking the pump
        out of the best sun of the day. When the panels cover the pump outright the battery
        is not the energy source, so the SOC gate is measuring the wrong thing. The floor
        still applies — surplus can vanish behind a cloud, and a start commits a compressor
        run that cannot be cancelled.
        """
        if not self._config.daytime_surplus_turn_on_enabled:
            return False
        if projected_surplus_kw is None:
            return False
        if battery_soc <= self._config.daytime_surplus_floor_soc:
            return False
        return projected_surplus_kw >= self._config.daytime_surplus_margin_kw

    def _daytime_thresholds(self, *, liberal_factor: float | None) -> "_DaytimeThresholds":
        factor = liberal_factor or 0.0
        return _DaytimeThresholds(
            turn_on_soc=linear_interpolate(
                self._config.battery_min_soc,
                self._config.battery_soft_min_soc + 5.0,
                factor,
            ),
            turn_off_soc=linear_interpolate(
                self._config.battery_min_soc,
                self._config.battery_soft_min_soc,
                factor,
            ),
        )

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
