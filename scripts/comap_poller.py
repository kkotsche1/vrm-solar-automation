"""Poll a ComAp InteliLite4 generator controller over Modbus TCP.

Reads generator voltage/frequency/current/power/RPM once per second and
appends each snapshot to a daily gzipped JSONL log, so the generator's own
view of the next E006 event can be cross-referenced against the Cerbo data.

The register map below is the standard ComAp IL-NT/InteliLite4 Modbus
holding-register layout (function code 3). Verify it against your controller
with --scan and --once before leaving the poller running; if your firmware
differs, override the map by editing REGISTERS (or tell me the correct
addresses).

Examples:

  python3 scripts/comap_poller.py --host 192.168.1.50 --once
  python3 scripts/comap_poller.py --host 192.168.1.50 --scan
  python3 scripts/comap_poller.py --host 192.168.1.50
"""

from __future__ import annotations

import argparse
import gzip
import json
import signal
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

# (name, address, scale, signed)
REGISTERS = [
    ("state",  0x0001, 1.0, False),
    ("vg1",    0x0014, 0.1, False),
    ("vg2",    0x0015, 0.1, False),
    ("vg3",    0x0016, 0.1, False),
    ("vg12",   0x0017, 0.1, False),
    ("vg23",   0x0018, 0.1, False),
    ("vg31",   0x0019, 0.1, False),
    ("freq",   0x001A, 0.1, False),
    ("il1",    0x001B, 0.1, False),
    ("il2",    0x001C, 0.1, False),
    ("il3",    0x001D, 0.1, False),
    ("p_kw",   0x001E, 1.0, True),
    ("q_kvar", 0x001F, 1.0, True),
    ("s_kva",  0x0020, 1.0, False),
    ("pf",     0x0021, 0.01, True),
    ("rpm",    0x0022, 1.0, False),
    ("batt_v", 0x0028, 0.1, False),
    ("oil_p",  0x0029, 0.1, False),
    ("eng_t",  0x002A, 1.0, False),
]

STATE_LABELS = {
    0: "init", 1: "ready", 2: "prestart", 3: "cranking", 4: "pause",
    5: "starting", 6: "idle", 7: "running", 8: "loaded", 9: "stop",
    10: "shutdown", 13: "cooling",
}


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            raise RuntimeError("socket closed before full Modbus response")
        buffer.extend(chunk)
    return bytes(buffer)


def read_registers(
    host: str,
    port: int,
    unit_id: int,
    address: int,
    count: int,
    *,
    function: int = 3,
    timeout: float = 5.0,
) -> list[int]:
    tid = int(time.time() * 1000) & 0xFFFF
    request = struct.pack(">HHHBBHH", tid, 0, 6, unit_id, function, address, count)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        response = _recv_exact(sock, 9 + 2 * count)

    rtid, rpid, rlen, runit, rfunc, rcount = struct.unpack(">HHHBBB", response[:9])
    if rtid != tid or rpid != 0 or runit != unit_id:
        raise RuntimeError("unexpected Modbus MBAP header")
    if rfunc & 0x80:
        raise RuntimeError(f"Modbus exception code {response[8]}")
    if rfunc != function or rcount != 2 * count:
        raise RuntimeError("unexpected Modbus function/byte count")
    return list(struct.unpack(f">{count}H", response[9 : 9 + 2 * count]))


def _signed(value: int) -> int:
    return value - 65536 if value >= 32768 else value


def poll(host: str, port: int, unit_id: int, function: int) -> dict[str, float | int]:
    start = REGISTERS[0][1]
    end = REGISTERS[-1][1]
    raw = read_registers(host, port, unit_id, start, end - start + 1, function=function)
    snapshot: dict[str, float | int] = {}
    for name, addr, scale, is_signed in REGISTERS:
        value = raw[addr - start]
        if is_signed:
            value = _signed(value)
        snapshot[name] = round(value * scale, 3)
    return snapshot


def format_snapshot(snap: dict) -> str:
    state = snap.get("state")
    label = STATE_LABELS.get(int(state), "?") if isinstance(state, (int, float)) else "?"
    return (
        f"state={state}({label}) Vg={snap['vg1']}/{snap['vg2']}/{snap['vg3']}V "
        f"f={snap['freq']}Hz I={snap['il1']}/{snap['il2']}/{snap['il3']}A "
        f"P={snap['p_kw']}kW RPM={snap['rpm']}"
    )


class ComapPoller:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.log_dir = Path(args.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._file = None
        self._file_day: str | None = None

    def _write(self, record: dict) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        if self._file is None or day != self._file_day:
            if self._file is not None:
                self._file.close()
            path = self.log_dir / f"comap-{day}.jsonl.gz"
            self._file = gzip.open(path, "ab")
            self._file_day = day
        line = json.dumps(record, separators=(",", ":")) + "\n"
        self._file.write(line.encode("utf-8"))
        self._file.flush()

    def run(self) -> None:
        args = self.args
        print(f"[comap] Modbus TCP {args.host}:{args.port} unit={args.unit_id}")
        print(f"[comap] logging to {self.log_dir}/comap-<date>.jsonl.gz")

        def request_stop(signum, frame) -> None:
            self._stop = True

        self._stop = False
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

        last_status = 0.0
        backoff = 1.0
        while not self._stop:
            start = time.time()
            try:
                snap = poll(args.host, args.port, args.unit_id, args.function)
                self._write({"t": time.time(), "comap": snap})
                backoff = 1.0
                if time.time() - last_status > 60:
                    print(f"[comap] {format_snapshot(snap)}")
                    last_status = time.time()
            except (OSError, TimeoutError, RuntimeError) as exc:
                if time.time() - last_status > 60:
                    print(f"[comap] read failed: {exc}", file=sys.stderr)
                    last_status = time.time()
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            elapsed = time.time() - start
            time.sleep(max(0.0, args.interval - elapsed))

        if self._file is not None:
            self._file.close()
            self._file = None
        print("[comap] stopped")


def scan(host: str, port: int, unit_id: int, function: int, start: int, count: int) -> None:
    print(f"scanning registers 0x{start:04X}..0x{start+count-1:04X} (function {function})")
    try:
        raw = read_registers(host, port, unit_id, start, count, function=function)
    except (OSError, TimeoutError, RuntimeError) as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        sys.exit(1)
    for i, value in enumerate(raw):
        addr = start + i
        if value not in (0, 0xFFFF):
            print(f"  0x{addr:04X} ({addr:4d}) = {value}  (signed {_signed(value)})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll a ComAp InteliLite4 over Modbus TCP.")
    parser.add_argument("--host", required=True, help="ComAp Modbus TCP host (Ethernet IP).")
    parser.add_argument("--port", type=int, default=502, help="Modbus TCP port (default 502).")
    parser.add_argument("--unit-id", type=int, default=1, help="Modbus slave/unit id (default 1).")
    parser.add_argument("--function", type=int, default=3, help="Modbus function: 3=holding, 4=input (default 3).")
    parser.add_argument("--interval", type=float, default=1.0, help="Poll interval seconds (default 1.0).")
    parser.add_argument("--log-dir", default="logs", help="Output directory (default logs).")
    parser.add_argument("--once", action="store_true", help="Poll once, print, and exit.")
    parser.add_argument("--scan", action="store_true", help="Scan a register range and exit.")
    parser.add_argument("--scan-start", type=lambda x: int(x, 0), default=0x0000, help="Scan start address (default 0x0000).")
    parser.add_argument("--scan-count", type=int, default=64, help="Number of registers to scan (default 64).")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.scan:
        scan(args.host, args.port, args.unit_id, args.function, args.scan_start, args.scan_count)
        return

    if args.once:
        snap = poll(args.host, args.port, args.unit_id, args.function)
        print(format_snapshot(snap))
        return

    ComapPoller(args).run()


if __name__ == "__main__":
    main()
