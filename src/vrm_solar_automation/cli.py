from __future__ import annotations

import argparse
import asyncio
import json

from .client import VrmProbeClient
from .config import load_settings
from .shelly import ShellyPlugClient
from .system import PumpControlSystem


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query Victron VRM and evaluate the circulation pump policy.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the environment file containing Victron credentials.",
    )
    subparsers = parser.add_subparsers(dest="command")

    metrics_parser = subparsers.add_parser(
        "metrics",
        help="Fetch the current VRM metrics.",
    )
    metrics_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the snapshot as JSON.",
    )

    decide_parser = subparsers.add_parser(
        "decide",
        help="Fetch VRM and weather data, then evaluate the pump policy.",
    )
    decide_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the decision payload as JSON.",
    )

    control_parser = subparsers.add_parser(
        "control",
        help="Evaluate the policy and reconcile the Shelly plug with the intended state.",
    )
    control_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the control payload as JSON.",
    )

    override_status_parser = subparsers.add_parser(
        "override-status",
        help="Show the currently stored temporary override state.",
    )
    override_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the override status as JSON.",
    )

    override_on_parser = subparsers.add_parser(
        "override-on",
        help="Force the pump on temporarily.",
    )
    override_on_parser.add_argument(
        "--minutes",
        type=float,
        required=True,
        help="How long the temporary manual-on override should stay active.",
    )
    override_on_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the override result as JSON.",
    )

    override_off_parser = subparsers.add_parser(
        "override-off",
        help="Force the pump off temporarily.",
    )
    override_off_parser.add_argument(
        "--minutes",
        type=float,
        required=True,
        help="How long the temporary manual-off override should stay active.",
    )
    override_off_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the override result as JSON.",
    )

    override_cycle_parser = subparsers.add_parser(
        "override-off-until-auto-on",
        help="Keep the pump off until the next fresh automatic ON signal.",
    )
    override_cycle_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the override result as JSON.",
    )

    override_clear_parser = subparsers.add_parser(
        "override-clear",
        help="Clear the current temporary override and return to automatic control.",
    )
    override_clear_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the clear result as JSON.",
    )

    plug_info_parser = subparsers.add_parser(
        "plug-info",
        help="Fetch Shelly device information.",
    )
    plug_info_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the device info as JSON.",
    )

    plug_status_parser = subparsers.add_parser(
        "plug-status",
        help="Fetch the current Shelly switch status.",
    )
    plug_status_parser.add_argument(
        "--switch-id",
        type=int,
        default=None,
        help="Override the configured Shelly switch component id.",
    )
    plug_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the switch status as JSON.",
    )

    for command_name, help_text, toggle_help in (
        (
            "plug-on",
            "Turn the Shelly switch on.",
            "Ask the Shelly device to turn itself off after this many seconds.",
        ),
        (
            "plug-off",
            "Turn the Shelly switch off.",
            "Ask the Shelly device to turn itself back on after this many seconds.",
        ),
    ):
        action_parser = subparsers.add_parser(command_name, help=help_text)
        action_parser.add_argument(
            "--switch-id",
            type=int,
            default=None,
            help="Override the configured Shelly switch component id.",
        )
        action_parser.add_argument(
            "--toggle-after-seconds",
            type=float,
            default=None,
            help=toggle_help,
        )
        action_parser.add_argument(
            "--json",
            action="store_true",
            help="Print the command result as JSON.",
        )

    plug_test_parser = subparsers.add_parser(
        "plug-test",
        help="Turn the Shelly switch on and let the Shelly device turn it off later.",
    )
    plug_test_parser.add_argument(
        "--switch-id",
        type=int,
        default=None,
        help="Override the configured Shelly switch component id.",
    )
    plug_test_parser.add_argument(
        "--on-seconds",
        type=float,
        default=30.0,
        help="How long to keep the plug on before turning it off again.",
    )
    plug_test_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the combined test result as JSON.",
    )

    return parser


async def _run_metrics(env_file: str, as_json: bool) -> int:
    settings = load_settings(env_file)
    snapshot = await VrmProbeClient(settings).fetch_snapshot()

    if as_json:
        print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
        return 0

    print(f"Site: {snapshot.site_name} ({snapshot.site_id})")
    print(f"Battery SOC: {snapshot.battery_soc_percent:.1f}%" if snapshot.battery_soc_percent is not None else "Battery SOC: unavailable")
    print(f"Solar watts: {snapshot.solar_watts:.0f} W" if snapshot.solar_watts is not None else "Solar watts: unavailable")
    print(f"House watts: {snapshot.house_watts:.0f} W" if snapshot.house_watts is not None else "House watts: unavailable")
    if snapshot.queried_at_unix_ms is not None:
        print(f"Sample timestamp: {snapshot.queried_at_unix_ms} ({snapshot.queried_at_iso})")
    else:
        print("Sample timestamp: unavailable")
    return 0


async def _run_decision(env_file: str, as_json: bool) -> int:
    settings = load_settings(env_file)
    decision, payload = await PumpControlSystem(settings).evaluate()

    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    power = payload["power"]
    weather = payload["weather"]
    override = payload["override"]
    print(f"Decision: {decision.action}")
    print("Target state: ON" if decision.should_turn_on else "Target state: OFF")
    print(f"Why: {decision.reason}")
    print(
        f"Battery {power['battery_soc_percent']:.1f}% | Solar {power['solar_watts']:.0f} W | House {power['house_watts']:.0f} W"
    )
    if power["generator_watts"] is not None:
        print(f"Generator watts: {power['generator_watts']:.0f} W")
    print(f"Weather today: {_format_weather_summary(weather)}")
    print(f"Effective target: {'ON' if override['effective_target_is_on'] else 'OFF'}")
    if override["is_active"]:
        print(f"Override: {_format_override_summary(override)}")
    return 0


async def _run_control(env_file: str, as_json: bool) -> int:
    settings = load_settings(env_file)
    decision, payload = await PumpControlSystem(settings).control()

    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    power = payload["power"]
    weather = payload["weather"]
    actuation = payload["actuation"]
    override = payload["override"]
    print(f"Decision: {decision.action}")
    print("Automatic target: ON" if decision.should_turn_on else "Automatic target: OFF")
    print(f"Why: {decision.reason}")
    print(
        f"Battery {power['battery_soc_percent']:.1f}% | Solar {power['solar_watts']:.0f} W | House {power['house_watts']:.0f} W"
    )
    if power["generator_watts"] is not None:
        print(f"Generator watts: {power['generator_watts']:.0f} W")
    print(f"Weather today: {_format_weather_summary(weather)}")
    print("Effective target: ON" if override["effective_target_is_on"] else "Effective target: OFF")
    if override["is_active"]:
        print(f"Override: {_format_override_summary(override)}")
    print(f"Actuation status: {actuation['status']}")
    if actuation["command_sent"]:
        print(f"Command sent: {actuation['command_sent']}")
    if actuation["observed_after_is_on"] is not None:
        print("Plug state after control: ON" if actuation["observed_after_is_on"] else "Plug state after control: OFF")
    if actuation["error"]:
        print(f"Actuation error: {actuation['error']}")
    return 0


async def _run_override_status(env_file: str, as_json: bool) -> int:
    payload = PumpControlSystem(load_settings(env_file)).read_override()

    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(_format_override_summary(payload))
    return 0


async def _run_override_set(
    env_file: str,
    *,
    mode: str,
    minutes: float | None,
    as_json: bool,
) -> int:
    system = PumpControlSystem(load_settings(env_file))
    if mode == "on":
        payload = await system.set_manual_on_override(duration_minutes=float(minutes))
    elif mode == "off":
        payload = await system.set_manual_off_override(duration_minutes=float(minutes))
    elif mode == "off_until_auto_on":
        payload = await system.set_manual_off_until_next_auto_on_override()
    else:
        payload = await system.clear_override()

    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Override: {_format_override_summary(payload['override'])}")
    if payload.get("actuation"):
        actuation = payload["actuation"]
        print(f"Actuation status: {actuation['status']}")
        if actuation.get("command_sent"):
            print(f"Command sent: {actuation['command_sent']}")
        if actuation.get("error"):
            print(f"Actuation error: {actuation['error']}")
    return 0


def _build_shelly_client(env_file: str) -> ShellyPlugClient:
    settings = load_settings(env_file)
    return ShellyPlugClient(settings.shelly_settings())


async def _run_plug_info(env_file: str, as_json: bool) -> int:
    info = await _build_shelly_client(env_file).fetch_device_info()

    if as_json:
        print(json.dumps(info.to_dict(), indent=2, sort_keys=True))
        return 0

    print(f"Device id: {info.device_id}")
    print(f"Name: {info.name or 'unnamed'}")
    print(f"Model: {info.model or 'unknown'}")
    print(f"Generation: {info.generation if info.generation is not None else 'unknown'}")
    print(f"Application: {info.application or 'unknown'}")
    print(f"Version: {info.version or 'unknown'}")
    print("Authentication: enabled" if info.auth_enabled else "Authentication: disabled")
    if info.auth_domain:
        print(f"Auth domain: {info.auth_domain}")
    return 0


async def _run_plug_status(env_file: str, switch_id: int | None, as_json: bool) -> int:
    status = await _build_shelly_client(env_file).fetch_switch_status(switch_id=switch_id)

    if as_json:
        print(json.dumps(status.to_dict(), indent=2, sort_keys=True))
        return 0

    print(f"Switch id: {status.switch_id}")
    print("Output: ON" if status.output else "Output: OFF")
    print(f"Source: {status.source or 'unknown'}")
    print(f"Power: {status.power_watts:.1f} W" if status.power_watts is not None else "Power: unavailable")
    print(f"Voltage: {status.voltage_volts:.1f} V" if status.voltage_volts is not None else "Voltage: unavailable")
    print(f"Current: {status.current_amps:.3f} A" if status.current_amps is not None else "Current: unavailable")
    print(
        f"Temperature: {status.temperature_c:.1f} C"
        if status.temperature_c is not None
        else "Temperature: unavailable"
    )
    return 0


async def _run_plug_action(
    env_file: str,
    *,
    turn_on: bool,
    switch_id: int | None,
    toggle_after_seconds: float | None,
    as_json: bool,
) -> int:
    client = _build_shelly_client(env_file)
    if turn_on:
        result = await client.turn_on(
            switch_id=switch_id,
            auto_off_seconds=toggle_after_seconds,
        )
    else:
        result = await client.turn_off(
            switch_id=switch_id,
            auto_on_seconds=toggle_after_seconds,
        )

    if as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    print(f"Switch id: {result.switch_id}")
    print("Requested state: ON" if result.requested_on else "Requested state: OFF")
    if result.was_on is not None:
        print("Previous state: ON" if result.was_on else "Previous state: OFF")
    print("Reported output: ON" if result.output else "Reported output: OFF")
    if toggle_after_seconds is not None:
        if turn_on:
            print(f"Auto-off after: {toggle_after_seconds:.1f} seconds")
        else:
            print(f"Auto-on after: {toggle_after_seconds:.1f} seconds")
    print(f"Source: {result.source or 'unknown'}")
    print(f"Executed at: {result.executed_at_iso}")
    return 0


async def _run_plug_test(
    env_file: str,
    *,
    switch_id: int | None,
    on_seconds: float,
    as_json: bool,
) -> int:
    client = _build_shelly_client(env_file)
    normalized_on_seconds = float(on_seconds)
    if normalized_on_seconds <= 0:
        raise ValueError("--on-seconds must be greater than zero.")

    before = await client.fetch_switch_status(switch_id=switch_id)
    turn_on_result = await client.turn_on_for(
        normalized_on_seconds,
        switch_id=switch_id,
    )
    # The off-timer is executed by the Shelly device itself; this wait is only
    # to verify the final state after the device-side timer should have elapsed.
    await asyncio.sleep(normalized_on_seconds + 1.0)
    after = await client.fetch_switch_status(switch_id=switch_id)

    payload = {
        "before": before.to_dict(),
        "armed_timer": turn_on_result.to_dict(),
        "after": after.to_dict(),
        "on_seconds": normalized_on_seconds,
    }

    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("Initial state: ON" if before.output else "Initial state: OFF")
    print(f"Turned on for: {normalized_on_seconds:.1f} seconds")
    print("Final state: ON" if after.output else "Final state: OFF")
    return 0


def _format_weather_summary(weather: dict[str, object]) -> str:
    return (
        f"low {_format_optional_float(weather['today_min_temperature_c'])} C, "
        f"high {_format_optional_float(weather['today_max_temperature_c'])} C, "
        f"current {_format_optional_float(weather['current_temperature_c'])} C"
    )


def _format_optional_float(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.1f}"


def _format_override_summary(override: dict[str, object]) -> str:
    if not bool(override["is_active"]):
        return "automatic mode"

    mode = str(override["mode"])
    if mode == "manual_on_until":
        return f"manual ON until {override['until_iso']}"
    if mode == "manual_off_until":
        return f"manual OFF until {override['until_iso']}"
    if mode == "manual_off_until_next_auto_on":
        if bool(override["seen_auto_off"]):
            return "manual OFF until the next fresh automatic ON signal"
        return "manual OFF waiting for an automatic OFF, then the next fresh automatic ON"
    return mode


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "metrics"
    if command == "metrics":
        raise SystemExit(asyncio.run(_run_metrics(args.env_file, args.json)))
    if command == "decide":
        raise SystemExit(asyncio.run(_run_decision(args.env_file, args.json)))
    if command == "control":
        raise SystemExit(asyncio.run(_run_control(args.env_file, args.json)))
    if command == "override-status":
        raise SystemExit(asyncio.run(_run_override_status(args.env_file, args.json)))
    if command == "override-on":
        raise SystemExit(
            asyncio.run(
                _run_override_set(
                    args.env_file,
                    mode="on",
                    minutes=args.minutes,
                    as_json=args.json,
                )
            )
        )
    if command == "override-off":
        raise SystemExit(
            asyncio.run(
                _run_override_set(
                    args.env_file,
                    mode="off",
                    minutes=args.minutes,
                    as_json=args.json,
                )
            )
        )
    if command == "override-off-until-auto-on":
        raise SystemExit(
            asyncio.run(
                _run_override_set(
                    args.env_file,
                    mode="off_until_auto_on",
                    minutes=None,
                    as_json=args.json,
                )
            )
        )
    if command == "override-clear":
        raise SystemExit(
            asyncio.run(
                _run_override_set(
                    args.env_file,
                    mode="clear",
                    minutes=None,
                    as_json=args.json,
                )
            )
        )
    if command == "plug-info":
        raise SystemExit(asyncio.run(_run_plug_info(args.env_file, args.json)))
    if command == "plug-status":
        raise SystemExit(asyncio.run(_run_plug_status(args.env_file, args.switch_id, args.json)))
    if command == "plug-on":
        raise SystemExit(
            asyncio.run(
                _run_plug_action(
                    args.env_file,
                    turn_on=True,
                    switch_id=args.switch_id,
                    toggle_after_seconds=args.toggle_after_seconds,
                    as_json=args.json,
                )
            )
        )
    if command == "plug-off":
        raise SystemExit(
            asyncio.run(
                _run_plug_action(
                    args.env_file,
                    turn_on=False,
                    switch_id=args.switch_id,
                    toggle_after_seconds=args.toggle_after_seconds,
                    as_json=args.json,
                )
            )
        )
    if command == "plug-test":
        raise SystemExit(
            asyncio.run(
                _run_plug_test(
                    args.env_file,
                    switch_id=args.switch_id,
                    on_seconds=args.on_seconds,
                    as_json=args.json,
                )
            )
        )
    raise SystemExit(2)
