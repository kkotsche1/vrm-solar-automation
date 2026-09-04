"""Replay logged nights through the old and new overnight reserve models.

The point of comparison is what actually went wrong in production: the battery reaching
the generator's auto-start band before solar took over. Because changing the thresholds
changes the pump schedule, and the pump schedule changes the SOC trace, a straight replay
of the logged SOC would not answer the question — so each night is simulated forward from
its logged 19:00 SOC using load figures measured from the same capture.

The new model is exercised through the real `PumpControlSystem` decision path rather than
a reimplementation; only the retired formula is restated here, in `_old_thresholds`.

Usage:
    python scripts/night_reserve_backtest.py [--db PATH] [--tz-offset HOURS]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import statistics
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vrm_solar_automation.config import Settings, load_settings  # noqa: E402
from vrm_solar_automation.models import PowerSnapshot  # noqa: E402
from vrm_solar_automation.policy import PumpPolicyState, linear_interpolate  # noqa: E402
from vrm_solar_automation.system import PumpControlSystem  # noqa: E402
from vrm_solar_automation.weather import WeatherSnapshot  # noqa: E402

# Generator auto-start is voltage driven, so its effective SOC trip point rises with load.
# Observed in the capture: 20-22% at rest, up to 28% under a running compressor.
GENERATOR_TRIP_AT_REST = 22.0
GENERATOR_TRIP_UNDER_LOAD = 28.0

OLD = dict(capacity_kwh=50.0, base_load_kw=1.5, floor=22.5, resume_hhmm=(7, 0),
           pump_load_kw=4.5, forced_off=((23, 30), (3, 30)), margin=(10.0, 7.0))


@dataclass
class Night:
    label: str
    start: datetime           # local 19:00
    crossover: datetime       # measured, battery first net-positive on solar
    start_soc: float
    today_sunshine: float
    tomorrow_sunshine: float
    sunrise_iso: str | None
    actual_min_soc: float
    actual_generator_ran: bool
    actual_pump_hours: float


def _fetch_sunrise(latitude: float, longitude: float, timezone: str,
                   past_days: int = 60) -> dict[str, str]:
    """Sunrise per local date, so the backtest resolves crossover the way production does."""
    query = urllib.parse.urlencode({
        "latitude": latitude, "longitude": longitude, "timezone": timezone,
        "daily": "sunrise", "past_days": min(past_days, 92), "forecast_days": 1,
    })
    try:
        with urllib.request.urlopen(
            f"https://api.open-meteo.com/v1/forecast?{query}", timeout=20
        ) as response:
            daily = json.load(response)["daily"]
    except Exception as exc:  # noqa: BLE001 - offline is a normal way to run this
        print(f"(sunrise lookup unavailable, using the fixed fallback crossover: {exc})\n")
        return {}
    return dict(zip(daily["time"], daily["sunrise"]))


def _load(db: str, tz_offset: int) -> tuple[list[Night], dict[int, float], float]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT timestamp_iso, battery_soc_percent, battery_power_w, solar_watts, "
        "generator_watts, plug_observed_is_on, today_sunshine_hours, tomorrow_sunshine_hours "
        "FROM control_cycle WHERE battery_power_w IS NOT NULL ORDER BY timestamp_iso"
    ).fetchall()
    pts = [(datetime.fromisoformat(r["timestamp_iso"]) + timedelta(hours=tz_offset), r)
           for r in rows]

    # Measured loads. Base load is taken with the pump off, in the dark, off generator;
    # the pump increment is the median gap between pump-on and pump-off draw in the same
    # hour. Evening hours have no pump-off samples, so they fall back to the pump-on draw
    # less that increment - a method that reproduces 23:00's directly measured 1.19 kW.
    off_by_hour: dict[int, list[float]] = defaultdict(list)
    on_by_hour: dict[int, list[float]] = defaultdict(list)
    for t, r in pts:
        if (r["generator_watts"] or 0) > 100 or (r["solar_watts"] or 0) > 50:
            continue
        target = on_by_hour if r["plug_observed_is_on"] else off_by_hour
        target[t.hour].append(-r["battery_power_w"] / 1000.0)

    increments = [
        statistics.median(on_by_hour[h]) - statistics.median(off_by_hour[h])
        for h in sorted(set(on_by_hour) & set(off_by_hour))
        if len(on_by_hour[h]) >= 20 and len(off_by_hour[h]) >= 20
    ]
    pump_kw = statistics.median(increments)

    base_by_hour: dict[int, float] = {}
    for hour in list(range(19, 24)) + list(range(0, 10)):
        if len(off_by_hour.get(hour, [])) >= 20:
            base_by_hour[hour] = statistics.median(off_by_hour[hour])
        elif len(on_by_hour.get(hour, [])) >= 20:
            base_by_hour[hour] = max(0.2, statistics.median(on_by_hour[hour]) - pump_kw)
    fallback = statistics.median(list(base_by_hour.values()))
    for hour in list(range(19, 24)) + list(range(0, 10)):
        base_by_hour.setdefault(hour, fallback)

    # Nights, keyed by the evening date; a night runs 19:00 -> measured solar crossover.
    by_night: dict[str, list] = defaultdict(list)
    for t, r in pts:
        if t.hour >= 19 or t.hour < 12:
            by_night[(t - timedelta(hours=12)).strftime("%Y-%m-%d")].append((t, r))

    nights: list[Night] = []
    sunrise_by_date = _fetch_sunrise(39.707337, 2.791675, "Europe/Madrid")
    for label in sorted(by_night):
        seg = by_night[label]
        start = next((p for p in seg if p[0].hour == 19), None)
        if start is None or start[1]["battery_soc_percent"] is None:
            continue
        crossover = next(
            (t for t, r in seg
             if t > start[0] and r["battery_power_w"] > 0 and (r["solar_watts"] or 0) > 800),
            None,
        )
        if crossover is None:
            continue
        window = [(t, r) for t, r in seg if start[0] <= t <= crossover]
        socs = [r["battery_soc_percent"] for _, r in window if r["battery_soc_percent"] is not None]
        pump_hours = sum(
            (window[i + 1][0] - window[i][0]).total_seconds() / 3600.0
            for i in range(len(window) - 1)
            if window[i][1]["plug_observed_is_on"]
            and (window[i + 1][0] - window[i][0]).total_seconds() < 720
        )
        nights.append(Night(
            label=label,
            start=start[0],
            crossover=crossover,
            start_soc=start[1]["battery_soc_percent"],
            today_sunshine=start[1]["today_sunshine_hours"] or 0.0,
            tomorrow_sunshine=start[1]["tomorrow_sunshine_hours"] or 0.0,
            actual_min_soc=min(socs),
            actual_generator_ran=any((r["generator_watts"] or 0) > 100 for _, r in window),
            actual_pump_hours=pump_hours,
            # The night ends at the *next* day's sunrise.
            sunrise_iso=sunrise_by_date.get(
                (start[0] + timedelta(days=1)).strftime("%Y-%m-%d")
            ),
        ))
    return nights, base_by_hour, pump_kw


def _old_thresholds(local_now: datetime, reference_sunshine: float) -> tuple[float, float]:
    """The retired formula: base-load reserve counted down to AUTO_RESUME_START_LOCAL."""
    resume = local_now.replace(hour=OLD["resume_hhmm"][0], minute=OLD["resume_hhmm"][1],
                               second=0, microsecond=0)
    if local_now.hour >= 19:
        resume += timedelta(days=1)
    hours = max(0.0, (resume - local_now).total_seconds() / 3600.0)
    required = OLD["floor"] + (hours * OLD["base_load_kw"] / OLD["capacity_kwh"]) * 100.0

    (fs_h, fs_m), (fe_h, fe_m) = OLD["forced_off"]
    minutes = local_now.hour * 60 + local_now.minute
    start, end = fs_h * 60 + fs_m, fe_h * 60 + fe_m
    inside = minutes >= start or minutes < end
    forced_reserve = 0.0
    if inside:
        end_at = local_now.replace(hour=fe_h, minute=fe_m, second=0, microsecond=0)
        if minutes >= start:
            end_at += timedelta(days=1)
        remaining = max(0.0, (end_at - local_now).total_seconds() / 3600.0)
        forced_reserve = (remaining * OLD["pump_load_kw"] / OLD["capacity_kwh"]) * 100.0

    reserve = required + forced_reserve
    factor = min(1.0, max(0.0, (reference_sunshine - 9.0) / 3.0))
    margin = linear_interpolate(OLD["margin"][0], OLD["margin"][1], factor)
    return reserve, reserve + margin


def _weather_for(night: Night) -> WeatherSnapshot:
    return WeatherSnapshot(
        current_temperature_c=None, today_min_temperature_c=None,
        today_max_temperature_c=None, today_sunshine_hours=night.today_sunshine,
        weather_code=None, queried_timezone="Europe/Madrid",
        tomorrow_sunshine_hours=night.tomorrow_sunshine,
        today_sunrise_iso=night.sunrise_iso, tomorrow_sunrise_iso=night.sunrise_iso,
    )


def _new_thresholds(system: PumpControlSystem, local_now: datetime,
                    soc: float, pump_on: bool, night: Night) -> tuple[float, float]:
    weather = WeatherSnapshot(
        current_temperature_c=None, today_min_temperature_c=None,
        today_max_temperature_c=None, today_sunshine_hours=night.today_sunshine,
        weather_code=None, queried_timezone="Europe/Madrid",
        tomorrow_sunshine_hours=night.tomorrow_sunshine,
        today_sunrise_iso=night.sunrise_iso,
        tomorrow_sunrise_iso=night.sunrise_iso,
    )
    power = PowerSnapshot.with_timestamp(
        site_id=1, site_name="backtest", site_identifier="backtest",
        battery_soc_percent=soc, solar_watts=0.0, house_watts=0.0,
        generator_watts=0.0, battery_power_w=None,
        active_input_source=None,
        queried_at_unix_ms=int(local_now.timestamp() * 1000),
    )
    decision = system._decide_surplus_night(
        power=power, weather=weather,
        previous_state=PumpPolicyState(is_on=pump_on, changed_at_iso=local_now.isoformat()),
        local_now=local_now,
    )
    return (decision.effective_turn_off_soc_percent or 0.0,
            decision.effective_turn_on_soc_percent or 0.0)


def _simulate(night: Night, base_by_hour: dict[int, float], pump_kw: float,
              capacity_kwh: float, thresholds, step_minutes: int = 5) -> tuple[float, float, bool]:
    """Step the night forward, returning (min SOC, pump hours, generator would have run)."""
    soc = night.start_soc
    pump_on = False
    min_soc = soc
    pump_hours = 0.0
    tripped = False
    t = night.start
    step = timedelta(minutes=step_minutes)
    hours = step_minutes / 60.0
    while t < night.crossover:
        turn_off, turn_on = thresholds(t, soc, pump_on)
        pump_on = soc > turn_off if pump_on else soc >= turn_on
        draw = base_by_hour[t.hour] + (pump_kw if pump_on else 0.0)
        soc -= (draw * hours / capacity_kwh) * 100.0
        trip = GENERATOR_TRIP_UNDER_LOAD if pump_on else GENERATOR_TRIP_AT_REST
        if soc <= trip:
            tripped = True
        min_soc = min(min_soc, soc)
        if pump_on:
            pump_hours += hours
        t += step
    return min_soc, pump_hours, tripped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="solar_automation.db")
    parser.add_argument("--tz-offset", type=int, default=2,
                        help="hours from stored UTC to local wall clock")
    parser.add_argument("--env", default=".env",
                        help="settings file supplying the new model's parameters")
    parser.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                        help="override a Settings field, e.g. "
                             "--set surplus_night_generator_margin_soc_percent=5")
    args = parser.parse_args()

    nights, base_by_hour, pump_kw = _load(args.db, args.tz_offset)
    if not nights:
        print("No complete nights (19:00 -> measured solar crossover) in the capture.")
        return 1

    try:
        settings = load_settings(args.env)
    except (OSError, ValueError):
        settings = Settings(cerbo_host="backtest", database_url="sqlite://")
    settings = _apply_overrides(settings, args.set)
    capacity = settings.battery_capacity_kwh
    system = _build_system(settings)

    print("Measured loads driving the simulation")
    print(f"  pump increment: {pump_kw:.2f} kW")
    print("  base load by hour (kW): " + "  ".join(
        f"{h:02d}={base_by_hour[h]:.2f}" for h in list(range(19, 24)) + list(range(0, 9))))
    print(f"  capacity: {capacity:.0f} kWh   night floor: "
          f"{settings.battery_hard_min_soc_percent + settings.surplus_night_generator_margin_soc_percent:.1f}%")
    print(f"  generator trip band: {GENERATOR_TRIP_AT_REST:.0f}% at rest, "
          f"{GENERATOR_TRIP_UNDER_LOAD:.0f}% under load\n")

    header = (f"{'night':11} {'sunrise':>8}{'model':>7}{'meas':>6} {'SOC@19':>7} | "
              f"{'OLD min':>8}{'OLD pump':>9}{'OLD gen':>8} | "
              f"{'NEW min':>8}{'NEW pump':>9}{'NEW gen':>8} | {'actual':>19}")
    print(header)
    print("-" * len(header))

    totals = {"old_hours": 0.0, "new_hours": 0.0, "old_trip": 0, "new_trip": 0}
    for night in nights:
        old = _simulate(night, base_by_hour, pump_kw, OLD["capacity_kwh"],
                        lambda t, soc, on, n=night: _old_thresholds(
                            t, n.tomorrow_sunshine if t.hour >= 19 else n.today_sunshine))
        new = _simulate(night, base_by_hour, pump_kw, capacity,
                        lambda t, soc, on, n=night: _new_thresholds(system, t, soc, on, n))
        totals["old_hours"] += old[1]
        totals["new_hours"] += new[1]
        totals["old_trip"] += int(old[2])
        totals["new_trip"] += int(new[2])
        actual = (f"min {night.actual_min_soc:.0f}% {night.actual_pump_hours:.1f}h "
                  f"{'GEN' if night.actual_generator_ran else '---'}")
        resolved, source = system._solar_crossover_minutes(
            weather=_weather_for(night), local_now=night.start
        )
        del source
        print(f"{night.label:11} "
              f"{(night.sunrise_iso[-5:] if night.sunrise_iso else '-'):>8}"
              f"{f'{resolved // 60:02d}:{resolved % 60:02d}':>7}"
              f"{night.crossover.strftime('%H:%M'):>6} "
              f"{night.start_soc:6.0f}% | "
              f"{old[0]:7.1f}%{old[1]:8.1f}h{('GEN' if old[2] else '---'):>8} | "
              f"{new[0]:7.1f}%{new[1]:8.1f}h{('GEN' if new[2] else '---'):>8} | {actual:>19}")

    n = len(nights)
    print("-" * len(header))
    print(f"generator starts: old {totals['old_trip']}/{n}   new {totals['new_trip']}/{n}")
    print(f"pump hours/night: old {totals['old_hours']/n:.1f}   new {totals['new_hours']/n:.1f} "
          f"({(totals['new_hours'] - totals['old_hours']) / max(totals['old_hours'], 1e-9) * 100:+.0f}%)")
    return 0


def _apply_overrides(settings: Settings, overrides: list[str]) -> Settings:
    if not overrides:
        return settings
    fields = {f.name: f.type for f in dataclasses.fields(Settings)}
    changes: dict[str, object] = {}
    for override in overrides:
        name, _, raw = override.partition("=")
        if name not in fields:
            raise SystemExit(f"unknown Settings field: {name}")
        current = getattr(settings, name)
        changes[name] = type(current)(raw) if isinstance(current, (int, float)) else raw
    return dataclasses.replace(settings, **changes)


def _build_system(settings: Settings) -> PumpControlSystem:
    """A control system with the network clients stubbed out; only the policy is exercised."""
    class _Stub:
        async def fetch(self, *a, **k):  # pragma: no cover - never called
            raise NotImplementedError

    class _StubStore:
        def load(self):  # pragma: no cover - never called
            return None

        def save(self, *a, **k):  # pragma: no cover - never called
            return None

        def record_cycle(self, *a, **k):  # pragma: no cover - never called
            return None

    settings = dataclasses.replace(settings, database_auto_migrate=False)
    return PumpControlSystem(
        settings,
        probe_client=_Stub(),
        weather_client=_Stub(),
        state_store=_StubStore(),
        notifier=None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
