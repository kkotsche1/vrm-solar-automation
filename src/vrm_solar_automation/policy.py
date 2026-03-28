from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .models import PowerSnapshot
from .weather import WeatherSnapshot


@dataclass(frozen=True)
class PumpPolicyConfig:
    battery_off_below_soc: float = 55.0
    battery_resume_above_soc: float = 72.0
    solar_assist_min_watts: float = 2500.0
    solar_assist_battery_floor_soc: float = 65.0
    generator_on_block_watts: float = 100.0
    mild_day_min_c: float = 12.0
    mild_day_max_c: float = 26.0
    heating_day_max_c: float = 17.0
    cooling_day_min_c: float = 27.0
    preferred_heating_months: tuple[int, ...] = (11, 12, 1, 2, 3)
    preferred_cooling_months: tuple[int, ...] = (6, 7, 8, 9)
    minimum_state_hold_minutes: int = 20


@dataclass(frozen=True)
class PumpPolicyState:
    is_on: bool
    changed_at_iso: str
    last_known_plug_is_on: bool | None = None
    last_known_plug_at_iso: str | None = None
    last_actuation_error: str | None = None
    last_actuation_at_iso: str | None = None
    override_mode: str | None = None
    override_until_iso: str | None = None
    override_set_at_iso: str | None = None
    override_seen_auto_off: bool = False

    @property
    def changed_at(self) -> datetime:
        return datetime.fromisoformat(self.changed_at_iso)

    @property
    def override_until(self) -> datetime | None:
        if self.override_until_iso is None:
            return None
        return datetime.fromisoformat(self.override_until_iso)

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "is_on": self.is_on,
            "changed_at_iso": self.changed_at_iso,
            "last_known_plug_is_on": self.last_known_plug_is_on,
            "last_known_plug_at_iso": self.last_known_plug_at_iso,
            "last_actuation_error": self.last_actuation_error,
            "last_actuation_at_iso": self.last_actuation_at_iso,
            "override_mode": self.override_mode,
            "override_until_iso": self.override_until_iso,
            "override_set_at_iso": self.override_set_at_iso,
            "override_seen_auto_off": self.override_seen_auto_off,
        }


@dataclass(frozen=True)
class PumpDecision:
    should_turn_on: bool
    action: str
    reason: str
    reasons: list[str] = field(default_factory=list)
    weather_mode: str = "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "should_turn_on": self.should_turn_on,
            "action": self.action,
            "reason": self.reason,
            "reasons": self.reasons,
            "weather_mode": self.weather_mode,
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
        now: datetime | None = None,
    ) -> PumpDecision:
        now = now or datetime.now(UTC)
        reasons: list[str] = []
        weather_mode = self._classify_weather(weather, now)
        battery_soc = power.battery_soc_percent
        solar_watts = power.solar_watts or 0.0
        generator_watts = abs(power.generator_watts or 0.0)

        if battery_soc is None:
            return PumpDecision(
                should_turn_on=False,
                action=self._action(False, previous_state),
                reason="Battery SOC is unavailable, so the policy fails safe to off.",
                reasons=["Battery SOC is unavailable."],
                weather_mode=weather_mode,
            )

        if generator_watts >= self._config.generator_on_block_watts:
            reasons.append(
                f"Generator power is present at {generator_watts:.0f} W, so the pump should stay off."
            )
            return self._finalize(False, previous_state, reasons, weather_mode, now, bypass_hold=True)

        if weather_mode == "unknown":
            reasons.append(
                "Weather data is unavailable, so the policy is falling back to battery, solar, and generator conditions."
            )

        if battery_soc <= self._config.battery_off_below_soc:
            reasons.append(
                f"Battery SOC is low at {battery_soc:.1f}%, below the off threshold of {self._config.battery_off_below_soc:.1f}%."
            )
            return self._finalize(False, previous_state, reasons, weather_mode, now, bypass_hold=True)

        if weather_mode == "mild":
            reasons.append("Outdoor temperatures are in the mild range, so heating or cooling is not needed.")
            return self._finalize(False, previous_state, reasons, weather_mode, now)

        if previous_state and previous_state.is_on:
            if battery_soc >= self._config.battery_off_below_soc:
                reasons.append(
                    f"Pump was already on, weather still calls for {weather_mode}, and battery SOC remains above the shutdown threshold at {battery_soc:.1f}%."
                )
                return self._finalize(True, previous_state, reasons, weather_mode, now)

        if battery_soc >= self._config.battery_resume_above_soc:
            reasons.append(
                f"Battery SOC is healthy at {battery_soc:.1f}%, above the resume threshold of {self._config.battery_resume_above_soc:.1f}%."
            )
            return self._finalize(True, previous_state, reasons, weather_mode, now)

        if (
            battery_soc >= self._config.solar_assist_battery_floor_soc
            and solar_watts >= self._config.solar_assist_min_watts
        ):
            reasons.append(
                f"Solar production is strong at {solar_watts:.0f} W and battery SOC is {battery_soc:.1f}%, so the pump can opportunistically run."
            )
            return self._finalize(True, previous_state, reasons, weather_mode, now)

        reasons.append(
            "Battery and solar conditions are not strong enough to justify running the pump right now."
        )
        return self._finalize(False, previous_state, reasons, weather_mode, now)

    def _classify_weather(self, weather: WeatherSnapshot, now: datetime) -> str:
        low = weather.today_min_temperature_c
        high = weather.today_max_temperature_c
        current = weather.current_temperature_c
        month = now.month
        if low is None or high is None:
            return "unknown"
        if high >= self._config.cooling_day_min_c:
            return "cooling"
        if high <= self._config.heating_day_max_c or low <= self._config.mild_day_min_c:
            return "heating"
        if self._config.mild_day_min_c < low and high < self._config.mild_day_max_c:
            return "mild"
        if (
            month in self._config.preferred_cooling_months
            and current is not None
            and current >= self._config.mild_day_max_c
        ):
            return "cooling"
        if (
            month in self._config.preferred_heating_months
            and current is not None
            and current <= self._config.heating_day_max_c
        ):
            return "heating"
        return "mixed"

    def _finalize(
        self,
        target_on: bool,
        previous_state: PumpPolicyState | None,
        reasons: list[str],
        weather_mode: str,
        now: datetime,
        *,
        bypass_hold: bool = False,
    ) -> PumpDecision:
        if previous_state and previous_state.is_on != target_on and not bypass_hold:
            hold_until = previous_state.changed_at + timedelta(
                minutes=self._config.minimum_state_hold_minutes
            )
            if now < hold_until:
                reasons.append(
                    f"Minimum hold time is active until {hold_until.isoformat()}, so the previous state is kept."
                )
                target_on = previous_state.is_on

        return PumpDecision(
            should_turn_on=target_on,
            action=self._action(target_on, previous_state),
            reason=reasons[0],
            reasons=reasons,
            weather_mode=weather_mode,
        )

    @staticmethod
    def _action(target_on: bool, previous_state: PumpPolicyState | None) -> str:
        if previous_state is None:
            return "turn_on" if target_on else "turn_off"
        if previous_state.is_on == target_on:
            return "keep_on" if target_on else "keep_off"
        return "turn_on" if target_on else "turn_off"
