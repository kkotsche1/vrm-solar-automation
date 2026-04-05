from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import PowerSnapshot
from .weather import WeatherSnapshot


@dataclass(frozen=True)
class PumpPolicyConfig:
    battery_min_soc: float = 45.0
    generator_on_block_watts: float = 100.0
    heating_below_c: float = 12.0
    cooling_above_c: float = 26.0


@dataclass(frozen=True)
class PumpPolicyState:
    is_on: bool
    changed_at_iso: str
    quiet_hours_forced_off: bool = False
    last_known_plug_is_on: bool | None = None
    last_known_plug_at_iso: str | None = None
    last_actuation_error: str | None = None
    last_actuation_at_iso: str | None = None

    @property
    def changed_at(self) -> datetime:
        return datetime.fromisoformat(self.changed_at_iso)

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "is_on": self.is_on,
            "changed_at_iso": self.changed_at_iso,
            "quiet_hours_forced_off": self.quiet_hours_forced_off,
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
        del now
        weather_mode = self._classify_weather(weather)
        battery_soc = power.battery_soc_percent
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
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                reason=(
                    f"Generator power is present at {generator_watts:.0f} W, so the pump should stay off."
                ),
            )

        if weather_mode == "unknown":
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                reason="Forecast min/max temperatures are unavailable, so automatic control stays off.",
            )

        if weather_mode == "mild":
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                reason="Today's forecast stays inside the comfort band, so automatic demand is off.",
            )

        if battery_soc <= self._config.battery_min_soc:
            return self._decision(
                target_on=False,
                previous_state=previous_state,
                weather_mode=weather_mode,
                reason=(
                    f"Today's forecast calls for {weather_mode}, but battery SOC is {battery_soc:.1f}%, at or below the {self._config.battery_min_soc:.1f}% minimum, so the pump should stay off."
                ),
            )

        return self._decision(
            target_on=True,
            previous_state=previous_state,
            weather_mode=weather_mode,
            reason=(
                f"Today's forecast calls for {weather_mode}, and battery SOC is {battery_soc:.1f}%, above the {self._config.battery_min_soc:.1f}% minimum run threshold."
            ),
        )

    def _classify_weather(self, weather: WeatherSnapshot) -> str:
        low = weather.today_min_temperature_c
        high = weather.today_max_temperature_c
        if low is None or high is None:
            return "unknown"
        needs_heating = low <= self._config.heating_below_c
        needs_cooling = high >= self._config.cooling_above_c
        if needs_heating and needs_cooling:
            return "mixed"
        if needs_heating:
            return "heating"
        if needs_cooling:
            return "cooling"
        return "mild"

    def _decision(
        self,
        *,
        target_on: bool,
        previous_state: PumpPolicyState | None,
        weather_mode: str,
        reason: str,
    ) -> PumpDecision:
        return PumpDecision(
            should_turn_on=target_on,
            action=self._action(target_on, previous_state),
            reason=reason,
            reasons=[reason],
            weather_mode=weather_mode,
        )

    @staticmethod
    def _action(target_on: bool, previous_state: PumpPolicyState | None) -> str:
        if previous_state is None:
            return "turn_on" if target_on else "turn_off"
        if previous_state.is_on == target_on:
            return "keep_on" if target_on else "keep_off"
        return "turn_on" if target_on else "turn_off"
