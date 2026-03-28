from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import socket
import struct

from .models import PowerSnapshot

SYSTEM_UNIT_ID = 100
REGISTER_TOTAL_CONSUMPTION_W = 817
REGISTER_GENSETS_L1_W = 823
REGISTER_GENSETS_L2_W = 824
REGISTER_GENSETS_L3_W = 825
REGISTER_ACTIVE_INPUT_SOURCE = 826
REGISTER_BATTERY_POWER_W = 842
REGISTER_BATTERY_SOC_PERCENT = 843
REGISTER_SOLAR_POWER_W = 850


class ModbusTcpClient:
    def __init__(self, host: str, port: int, timeout_seconds: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._timeout_seconds = timeout_seconds
        self._transaction_id = 0

    def read_input_register(self, *, unit_id: int, register: int) -> int:
        self._transaction_id = (self._transaction_id + 1) % 65536
        request = struct.pack(
            ">HHHBBHH",
            self._transaction_id,
            0,
            6,
            unit_id,
            4,
            register,
            1,
        )

        with socket.create_connection(
            (self._host, self._port),
            timeout=self._timeout_seconds,
        ) as sock:
            sock.sendall(request)
            response = _recv_exact(sock, 11)

        (
            transaction_id,
            protocol_id,
            length,
            response_unit_id,
            function_code,
            byte_count,
            value,
        ) = struct.unpack(">HHHBBBH", response)

        if transaction_id != self._transaction_id:
            raise RuntimeError("Unexpected Modbus transaction ID in response.")
        if protocol_id != 0:
            raise RuntimeError("Unexpected Modbus protocol ID in response.")
        if response_unit_id != unit_id:
            raise RuntimeError("Unexpected Modbus unit ID in response.")
        if function_code == 0x84:
            raise RuntimeError(f"Modbus exception while reading register {register}.")
        if function_code != 4:
            raise RuntimeError(f"Unexpected Modbus function code {function_code}.")
        if length != 5 or byte_count != 2:
            raise RuntimeError("Unexpected Modbus payload length.")

        return value


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            raise RuntimeError("Socket closed before full Modbus response was received.")
        buffer.extend(chunk)
    return bytes(buffer)


def uint16_to_int16(value: int) -> int:
    return value - 65536 if value >= 32768 else value


@dataclass(frozen=True)
class CerboSettings:
    host: str
    port: int
    site_name: str
    site_identifier: str
    site_id: int


class CerboProbeClient:
    def __init__(self, settings: CerboSettings) -> None:
        self._settings = settings
        self._client = ModbusTcpClient(host=settings.host, port=settings.port)

    async def fetch_snapshot(self) -> PowerSnapshot:
        battery_soc_raw = self._client.read_input_register(
            unit_id=SYSTEM_UNIT_ID,
            register=REGISTER_BATTERY_SOC_PERCENT,
        )
        battery_power_raw = self._client.read_input_register(
            unit_id=SYSTEM_UNIT_ID,
            register=REGISTER_BATTERY_POWER_W,
        )
        solar_power_raw = self._client.read_input_register(
            unit_id=SYSTEM_UNIT_ID,
            register=REGISTER_SOLAR_POWER_W,
        )
        total_consumption_raw = self._client.read_input_register(
            unit_id=SYSTEM_UNIT_ID,
            register=REGISTER_TOTAL_CONSUMPTION_W,
        )
        generator_phase_raws = [
            self._read_optional_input_register(REGISTER_GENSETS_L1_W),
            self._read_optional_input_register(REGISTER_GENSETS_L2_W),
            self._read_optional_input_register(REGISTER_GENSETS_L3_W),
        ]
        active_input_source = self._read_optional_input_register(REGISTER_ACTIVE_INPUT_SOURCE)

        # Battery power is read mainly to validate register coherence and for
        # future policy refinements, but the current snapshot exposes the core
        # control inputs only.
        _battery_power_w = uint16_to_int16(battery_power_raw)
        generator_watts = None
        if any(raw is not None for raw in generator_phase_raws):
            generator_watts = float(
                sum(uint16_to_int16(raw) for raw in generator_phase_raws if raw is not None)
            )
        queried_at_unix_ms = int(datetime.now(UTC).timestamp() * 1000)

        return PowerSnapshot.with_timestamp(
            site_id=self._settings.site_id,
            site_name=self._settings.site_name,
            site_identifier=self._settings.site_identifier,
            battery_soc_percent=float(battery_soc_raw),
            solar_watts=float(uint16_to_int16(solar_power_raw)),
            house_watts=float(uint16_to_int16(total_consumption_raw)),
            generator_watts=generator_watts,
            active_input_source=active_input_source,
            queried_at_unix_ms=queried_at_unix_ms,
        )

    def _read_optional_input_register(self, register: int) -> int | None:
        try:
            return self._client.read_input_register(
                unit_id=SYSTEM_UNIT_ID,
                register=register,
            )
        except RuntimeError:
            return None
