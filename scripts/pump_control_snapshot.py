from __future__ import annotations

import asyncio
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vrm_solar_automation.config import load_settings
from vrm_solar_automation.system import PumpControlSystem


async def main() -> None:
    env_file = Path(".env")
    settings = load_settings(env_file)
    decision, payload = await PumpControlSystem(settings).control()

    power = payload["power"]
    weather = payload["weather"]
    actuation = payload["actuation"]

    print("Victron state")
    print(f"  Site: {power['site_name']} ({power['site_id']})")
    print(f"  Battery SOC: {power['battery_soc_percent']:.1f}%")
    print(f"  Solar watts: {power['solar_watts']:.0f} W")
    print(f"  House watts: {power['house_watts']:.0f} W")
    if power["generator_watts"] is not None:
        print(f"  Generator watts: {power['generator_watts']:.0f} W")
    print(f"  Sample time: {power['queried_at_iso']}")
    print()
    print("Weather inputs")
    print(f"  Current temperature: {_format_optional_float(weather['current_temperature_c'])} C")
    print(f"  Today's low: {_format_optional_float(weather['today_min_temperature_c'])} C")
    print(f"  Today's high: {_format_optional_float(weather['today_max_temperature_c'])} C")
    print(f"  Weather code: {weather['weather_code']}")
    print(f"  Timezone: {weather['queried_timezone']}")
    print()
    print("Controller decision")
    print(f"  Action: {decision.action}")
    print(f"  Target pump state: {'ON' if decision.should_turn_on else 'OFF'}")
    print(f"  Weather mode: {decision.weather_mode}")
    print(f"  Reason: {decision.reason}")
    print()
    print("Shelly reconciliation")
    print(f"  Status: {actuation['status']}")
    if actuation["command_sent"]:
        print(f"  Command sent: {actuation['command_sent']}")
    if actuation["observed_after_is_on"] is not None:
        print(
            f"  Observed plug state after control: {'ON' if actuation['observed_after_is_on'] else 'OFF'}"
        )
    if actuation["error"]:
        print(f"  Error: {actuation['error']}")


def _format_optional_float(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.1f}"


if __name__ == "__main__":
    asyncio.run(main())
