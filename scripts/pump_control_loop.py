from __future__ import annotations

import argparse
import asyncio
import json
import signal
from datetime import UTC, datetime
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
        description="Run pump control continuously at a fixed interval.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the environment file containing controller settings.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=15.0,
        help="Loop interval in seconds (default: 15).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print each cycle payload as JSON.",
    )
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    if args.interval_seconds <= 0:
        raise SystemExit("--interval-seconds must be greater than zero.")

    settings = load_settings(args.env_file)
    system = PumpControlSystem(settings)

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    print(
        f"Starting continuous pump control loop (interval={args.interval_seconds:.1f}s, env={args.env_file})."
    )
    while not stop_event.is_set():
        started = datetime.now(UTC)
        try:
            decision, payload = await system.control()
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                _print_cycle_summary(started=started, decision_action=decision.action, payload=payload)
        except Exception as exc:
            print(f"[{started.isoformat()}] Control cycle failed: {exc}")

        elapsed = (datetime.now(UTC) - started).total_seconds()
        sleep_seconds = max(0.0, args.interval_seconds - elapsed)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_seconds)
        except TimeoutError:
            continue

    print("Pump control loop stopped.")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Fallback for platforms/event-loops that do not support signal handlers.
            signal.signal(sig, lambda _sig, _frame: stop_event.set())


def _print_cycle_summary(
    *,
    started: datetime,
    decision_action: str,
    payload: dict[str, object],
) -> None:
    actuation = payload["actuation"]
    intended_target_is_on = bool(payload["intended_target_is_on"])
    print(
        f"[{started.isoformat()}] decision={decision_action} "
        f"target={'ON' if intended_target_is_on else 'OFF'} "
        f"actuation={actuation['status']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
