from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vrm_solar_automation.config import load_settings
from vrm_solar_automation.system import PumpControlSystem


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one control cycle for the pump controller.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the environment file containing controller settings.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full control payload as JSON.",
    )
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings(args.env_file)
    decision, payload = await PumpControlSystem(settings).control()

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    power = payload["power"]
    weather = payload["weather"]
    actuation = payload["actuation"]

    print("Pump control cycle")
    print(f"  Decision: {decision.action}")
    print(f"  Automatic target: {'ON' if decision.should_turn_on else 'OFF'}")
    print(f"  Plug target: {'ON' if payload['intended_target_is_on'] else 'OFF'}")
    print(f"  Reason: {decision.reason}")
    if payload["quiet_hours_blocked"]:
        print(f"  Blocked: {payload['blocked_reason']}")
    print(f"  Battery SOC: {_format_optional_percent(power['battery_soc_percent'])}")
    print(f"  Solar watts: {_format_optional_watts(power['solar_watts'])}")
    print(f"  House watts: {_format_optional_watts(power['house_watts'])}")
    print(f"  Generator watts: {_format_optional_watts(power['generator_watts'])}")
    print(f"  Weather sunshine: {_format_optional_hours(weather['today_sunshine_hours'])}")
    print(f"  Weather current temp: {_format_optional_float(weather['current_temperature_c'])} C")
    print(f"  Actuation status: {actuation['status']}")
    if actuation["command_sent"]:
        print(f"  Command sent: {actuation['command_sent']}")
    if actuation["observed_after_is_on"] is not None:
        print(
            "  Plug state after control: ON"
            if actuation["observed_after_is_on"]
            else "  Plug state after control: OFF"
        )
    if actuation["error"]:
        print(f"  Actuation error: {actuation['error']}")


def _format_optional_float(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.1f}"


def _format_optional_hours(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.1f} h"


def _format_optional_percent(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.1f}%"


def _format_optional_watts(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.0f} W"


if __name__ == "__main__":
    asyncio.run(main())
