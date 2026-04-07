from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vrm_solar_automation.config import load_settings
from vrm_solar_automation.shelly import ShellyError, ShellyPlugClient
from vrm_solar_automation.system import PumpControlSystem


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run controller snapshot (no plug command is sent).",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: .env).",
    )
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings(args.env_file)
    decision, payload = await PumpControlSystem(settings).evaluate()

    power = payload["power"]
    power_status = payload["power_status"]
    intended_is_on = bool(payload["intended_target_is_on"])
    intended_command = "turn_on" if intended_is_on else "turn_off"

    plug_output: bool | None = None
    plug_error: str | None = None
    plug_source: str | None = None

    if settings.shelly_host:
        try:
            plug_status = await ShellyPlugClient(settings.shelly_settings()).fetch_switch_status()
            plug_output = bool(plug_status.output)
            plug_source = plug_status.source
        except ShellyError as exc:
            plug_error = str(exc)
    else:
        plug_error = "SHELLY_HOST is not configured."

    simulated_action = _build_simulated_action(
        power_available=bool(power_status.get("available")),
        intended_command=intended_command,
        intended_is_on=intended_is_on,
        plug_output=plug_output,
        plug_error=plug_error,
    )

    print("Pump dry-run snapshot (no actuation)")
    print()
    print("Cerbo power data")
    print(f"  Site: {power['site_name']} ({power['site_id']})")
    print(f"  Battery SOC: {_format_optional_percent(power['battery_soc_percent'])}")
    print(f"  Solar watts: {_format_optional_watts(power['solar_watts'])}")
    print(f"  House watts: {_format_optional_watts(power['house_watts'])}")
    print(f"  Generator watts: {_format_optional_watts(power['generator_watts'])}")
    print(f"  Power status available: {power_status['available']}")
    print(f"  Power status error: {power_status['error'] or 'none'}")
    print()
    print("Current plug state")
    if plug_error:
        print(f"  Reachable: no ({plug_error})")
    else:
        print("  Reachable: yes")
        print(f"  Output: {_format_on_off(plug_output)}")
        print(f"  Source: {plug_source or 'unknown'}")
    print()
    print("Controller decision (dry-run)")
    print(f"  Policy action: {decision.action}")
    print(f"  Automatic target: {_format_on_off(decision.should_turn_on)}")
    print(f"  Plug target: {_format_on_off(intended_is_on)}")
    print(f"  Quiet hours blocked: {'yes' if payload['quiet_hours_blocked'] else 'no'}")
    if payload["blocked_reason"]:
        print(f"  Block reason: {payload['blocked_reason']}")
    print(f"  Weather tomorrow sunshine: {_format_optional_hours(payload['weather']['tomorrow_sunshine_hours'])}")
    if payload["night_surplus_mode_active"]:
        print(
            f"  Night reserve: {_format_optional_percent(payload['night_required_soc_percent'])}"
        )
        print(
            "  Night reference sunshine: "
            f"{_format_optional_hours(payload['night_reference_sunshine_hours'])}"
        )
    print(f"  Decision reason: {decision.reason}")
    print()
    print("If live control ran now")
    print(f"  {simulated_action}")


def _build_simulated_action(
    *,
    power_available: bool,
    intended_command: str,
    intended_is_on: bool,
    plug_output: bool | None,
    plug_error: str | None,
) -> str:
    if not power_available:
        return "Would send no command because telemetry is unavailable."
    if plug_error is not None and plug_output is None:
        return f"Would try `{intended_command}`, but the plug is unreachable."
    if plug_output is None:
        return f"Would send `{intended_command}` (plug state unknown)."
    if plug_output == intended_is_on:
        return "No command would be needed because the plug is already aligned."
    return f"Would send `{intended_command}`."


def _format_on_off(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "ON" if value else "OFF"


def _format_optional_percent(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.1f}%"


def _format_optional_hours(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.1f} h"


def _format_optional_watts(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.0f} W"


if __name__ == "__main__":
    asyncio.run(main())
