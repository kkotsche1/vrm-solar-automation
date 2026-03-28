from __future__ import annotations

import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vrm_solar_automation.cerbo import (
    CerboProbeClient,
    ModbusTcpClient,
    REGISTER_BATTERY_POWER_W,
    SYSTEM_UNIT_ID,
    uint16_to_int16,
)
from vrm_solar_automation.config import load_settings


def main() -> None:
    settings = load_settings()
    host = settings.cerbo_host
    port = settings.cerbo_port
    client = ModbusTcpClient(host=host, port=port)

    try:
        snapshot = __import__("asyncio").run(
            CerboProbeClient(settings.cerbo_settings()).fetch_snapshot()
        )
        battery_power_raw = client.read_input_register(
            unit_id=SYSTEM_UNIT_ID,
            register=REGISTER_BATTERY_POWER_W,
        )
    except ConnectionRefusedError:
        print(f"Could not connect to Cerbo GX Modbus TCP at {host}:{port}.")
        print("The device refused the connection.")
        print("Check the following on the Cerbo GX:")
        print("  1. Modbus-TCP is enabled in Settings -> Services -> Modbus/TCP.")
        print("  2. The Cerbo IP address is still correct.")
        print("  3. Your Windows machine is on the same LAN as the Cerbo.")
        sys.exit(1)
    except TimeoutError:
        print(f"Timed out while connecting to Cerbo GX Modbus TCP at {host}:{port}.")
        print("Check the Cerbo IP address and LAN connectivity.")
        sys.exit(1)

    battery_power_w = uint16_to_int16(battery_power_raw)

    print("Cerbo GX Modbus snapshot")
    print(f"  Host: {host}:{port}")
    print(f"  Battery SOC: {snapshot.battery_soc_percent:.1f}%")
    print(f"  Battery power: {battery_power_w} W")
    print(f"  Solar power: {snapshot.solar_watts:.0f} W")
    print(f"  House load: {snapshot.house_watts:.0f} W")


if __name__ == "__main__":
    main()
