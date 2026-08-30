"""E006 heat-pump diagnostic logger.

Runs as a separate process alongside the VRM automation. It subscribes to the
Cerbo GX "MQTT on LAN" telemetry stream and writes every received message into
daily gzipped JSONL files, so nothing relevant to the E006 heat-pump fault is
lost. It only publishes the keepalive message the Cerbo broker requires; it
never writes to any device.

An optional Shelly acts as an independent 230 V voltage witness. Gen1 devices
report power only; Gen2/Gen3 also report voltage.

Two outputs are produced under the log directory:

  cerbo-YYYY-MM-DD.jsonl.gz  raw telemetry, one JSON object per line
  event.log                  plain-English, one line per derived state change
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - handled at runtime
    mqtt = None

try:
    import requests
except ImportError:  # pragma: no cover - handled at runtime
    requests = None

SOURCE_LABELS = {
    0: "UNKNOWN",
    1: "GRID",
    2: "GENERATOR",
    3: "SHORE",
    240: "NOT CONNECTED",
}

KEEPALIVE_TOPIC = "R/{portal}/keepalive"

RE_SOURCE = re.compile(r"^system/\d+/Ac/ActiveIn/Source$")
RE_SOC = re.compile(r"^system/\d+/Dc/Battery/Soc$")
RE_NUM_PHASES = re.compile(r"^system/\d+/Ac/Consumption/NumberOfPhases$")
RE_CONSUMPTION_PHASE = re.compile(r"^system/\d+/Ac/Consumption/(L\d)/Power$")

PHASES = ("L1", "L2", "L3")


def parse_payload(payload: bytes) -> str | int | float | bool | None:
    text = payload.decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return text


class E006Logger:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.log_dir = Path(args.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._file = None
        self._file_day: str | None = None

        self._portal_id: str | None = None
        self._portal_event = threading.Event()
        self._stop = threading.Event()
        self._keepalive_counter = 0
        self._mqtt_client = None
        self._client_id = args.client_id or f"e006-{int(time.time())}"

        self._last_source: int | None = None
        self._last_soc: float | None = None
        self._phase_power: dict[str, float] = {}
        self._num_phases: int | None = None
        self._hp_running = False
        self._hp_low: float | None = None
        self._hp_high: float | None = None
        self._hp_candidate: str | None = None
        self._hp_candidate_since: float | None = None
        self._last_error_at = 0.0

    def _write_record(self, record: dict) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            if self._file is None or day != self._file_day:
                if self._file is not None:
                    self._file.close()
                path = self.log_dir / f"cerbo-{day}.jsonl.gz"
                self._file = gzip.open(path, "ab")
                self._file_day = day
            line = json.dumps(record, separators=(",", ":")) + "\n"
            self._file.write(line.encode("utf-8"))
            self._file.flush()

    def _write_event(self, text: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp}  {text}\n"
        with self._lock:
            with (self.log_dir / "event.log").open("a", encoding="utf-8") as fh:
                fh.write(line)
        print(line.rstrip())

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            print(f"[mqtt] connect failed: {reason_code}", file=sys.stderr)
            return
        print("[mqtt] connected, subscribed to N/#")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if not self._stop.is_set():
            print(f"[mqtt] disconnected ({reason_code}), waiting to reconnect", file=sys.stderr)

    def _on_message(self, client, userdata, msg) -> None:
        topic = msg.topic
        parts = topic.split("/")
        if len(parts) >= 2 and parts[0] == "N" and self._portal_id is None:
            self._portal_id = parts[1]
            self._portal_event.set()
            print(f"[mqtt] discovered portal id: {self._portal_id}")

        value = parse_payload(msg.payload)
        self._write_record({"t": time.time(), "topic": topic, "v": value})
        self._handle_topic(topic, value)

    def _handle_topic(self, topic: str, value) -> None:
        parts = topic.split("/")
        if parts and parts[0] == "N":
            parts = parts[2:]
        path = "/".join(parts)

        match = RE_SOURCE.match(path)
        if match:
            self._on_source(value)
            return

        if RE_SOC.match(path):
            self._last_soc = _as_float(value)
            return

        if RE_NUM_PHASES.match(path):
            self._num_phases = _as_int(value)
            return

        match = RE_CONSUMPTION_PHASE.match(path)
        if match:
            watts = _as_float(value)
            if watts is not None:
                self._phase_power[match.group(1)] = watts
                self._on_consumption()

    def _on_source(self, value) -> None:
        source = _as_int(value)
        if source is None:
            return
        if self._last_source is None:
            self._last_source = source
            self._write_event(
                f"AC source -> {SOURCE_LABELS.get(source, source)} "
                f"(source={source}, SOC {_fmt(self._last_soc)})"
            )
            return
        if source != self._last_source:
            old = SOURCE_LABELS.get(self._last_source, self._last_source)
            new = SOURCE_LABELS.get(source, source)
            self._write_event(
                f"AC source changed {old} -> {new} "
                f"(source={source}, SOC {_fmt(self._last_soc)})"
            )
            self._last_source = source

    def _on_consumption(self) -> None:
        total = self._total_consumption()
        if total is None:
            return
        expected = self._num_phases if self._num_phases else 3
        if len(self._phase_power) < expected:
            return
        if self._hp_low is None:
            self._hp_low = total
            self._hp_high = total
            return

        on_step = self.args.hp_on_step_w
        off_step = self.args.hp_off_step_w
        debounce = self.args.hp_debounce_seconds
        now = time.monotonic()

        if not self._hp_running:
            if total < self._hp_low:
                self._hp_low = total
            if total >= self._hp_low + on_step:
                self._arm_candidate("started", now)
                if now - self._hp_candidate_since >= debounce:
                    self._hp_running = True
                    self._hp_high = total
                    self._clear_candidate()
                    self._write_event(self._hp_event("HEAT PUMP STARTED"))
            else:
                self._clear_candidate()
        else:
            if total > self._hp_high:
                self._hp_high = total
            if total <= self._hp_high - off_step:
                self._arm_candidate("stopped", now)
                if now - self._hp_candidate_since >= debounce:
                    self._hp_running = False
                    self._hp_low = total
                    self._clear_candidate()
                    self._write_event(self._hp_event("HEAT PUMP STOPPED"))
            else:
                self._clear_candidate()

    def _arm_candidate(self, label: str, now: float) -> None:
        if self._hp_candidate != label:
            self._hp_candidate = label
            self._hp_candidate_since = now

    def _clear_candidate(self) -> None:
        self._hp_candidate = None
        self._hp_candidate_since = None

    def _total_consumption(self) -> float | None:
        known = [self._phase_power[p] for p in PHASES if p in self._phase_power]
        return sum(known) if known else None

    def _hp_event(self, label: str) -> str:
        total = self._total_consumption()
        if self._last_source == 240:
            source_word = "inverter"
        else:
            source_word = SOURCE_LABELS.get(self._last_source, "UNKNOWN").lower()
        return (
            f"{label} on {source_word} "
            f"(total load {_fmt_watts(total)}, SOC {_fmt(self._last_soc)})"
        )

    def _keepalive_loop(self) -> None:
        self._portal_event.wait()
        interval = self.args.keepalive_seconds
        while not self._stop.is_set():
            client = self._mqtt_client
            if client is not None and client.is_connected() and self._portal_id:
                self._keepalive_counter += 1
                echo = f"{self._client_id}-{self._keepalive_counter}"
                payload = json.dumps(
                    {"keepalive-options": [{"full-publish-completed-echo": echo}]}
                )
                client.publish(
                    KEEPALIVE_TOPIC.format(portal=self._portal_id), payload
                )
            self._stop.wait(interval)

    def _shelly_loop(self) -> None:
        host = self.args.shelly
        if not host:
            return
        if requests is None:
            print("[shelly] requests not installed, skipping Shelly", file=sys.stderr)
            return
        url = f"http://{host}/rpc/Switch.GetStatus?id=0"
        interval = self.args.shelly_interval
        session = requests.Session()
        while not self._stop.is_set():
            start = time.time()
            try:
                data = session.get(url, timeout=5.0).json()
                record = {
                    "t": time.time(),
                    "shelly": {
                        "output": data.get("output"),
                        "apower": data.get("apower"),
                        "voltage": data.get("voltage"),
                        "current": data.get("current"),
                    },
                }
                temperature = data.get("temperature")
                if isinstance(temperature, dict) and temperature.get("tC") is not None:
                    record["shelly"]["temp"] = temperature["tC"]
                self._write_record(record)
            except Exception as exc:  # noqa: BLE001 - witness is best-effort
                now = time.time()
                if now - self._last_error_at > 60:
                    self._last_error_at = now
                    print(f"[shelly] read failed: {exc}", file=sys.stderr)
            elapsed = time.time() - start
            self._stop.wait(max(0.0, interval - elapsed))

    def run(self) -> None:
        if mqtt is None:
            print("paho-mqtt is not installed. Run: pip3 install paho-mqtt", file=sys.stderr)
            sys.exit(1)

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id)
        self._mqtt_client = client
        if self.args.username:
            client.username_pw_set(self.args.username, self.args.password)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=2, max_delay=60)

        client.connect(self.args.host, self.args.port, keepalive=60)
        client.subscribe("N/#", qos=0)
        client.loop_start()

        threads = [
            threading.Thread(target=self._keepalive_loop, name="keepalive", daemon=True),
        ]
        if self.args.shelly:
            threads.append(
                threading.Thread(target=self._shelly_loop, name="shelly", daemon=True)
            )
        for thread in threads:
            thread.start()

        print(
            f"[e006] logging to {self.log_dir}/cerbo-<date>.jsonl.gz and "
            f"{self.log_dir}/event.log"
        )
        print(f"[e006] Cerbo MQTT {self.args.host}:{self.args.port}")
        if self.args.shelly:
            print(f"[e006] Shelly witness {self.args.shelly}")

        def request_stop(signum, frame) -> None:
            self._stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

        while not self._stop.is_set():
            self._stop.wait(1.0)

        client.loop_stop()
        client.disconnect()
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
        print("[e006] stopped")


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}"


def _fmt_watts(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} W"


def _as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture Cerbo GX MQTT telemetry plus an optional Shelly "
        "voltage witness for E006 heat-pump diagnostics.",
    )
    parser.add_argument("--host", required=True, help="Cerbo GX IP address (MQTT on LAN).")
    parser.add_argument("--port", type=int, default=1883, help="MQTT port (default 1883).")
    parser.add_argument("--username", default=None, help="Optional MQTT username.")
    parser.add_argument("--password", default=None, help="Optional MQTT password.")
    parser.add_argument("--shelly", default=None, help="Optional Shelly IP address.")
    parser.add_argument(
        "--shelly-interval",
        type=float,
        default=1.0,
        help="Seconds between Shelly reads (default 1.0).",
    )
    parser.add_argument("--client-id", default=None, help="MQTT client id.")
    parser.add_argument(
        "--keepalive-seconds",
        type=float,
        default=30.0,
        help="Seconds between keepalive publishes (default 30.0).",
    )
    parser.add_argument(
        "--hp-on-step-w",
        type=float,
        default=900.0,
        help="Consumption rise (W) that marks the heat-pump starting (default 900).",
    )
    parser.add_argument(
        "--hp-off-step-w",
        type=float,
        default=900.0,
        help="Consumption drop (W) that marks the heat-pump stopping (default 900).",
    )
    parser.add_argument(
        "--hp-debounce-seconds",
        type=float,
        default=5.0,
        help="Seconds a heat-pump state change must persist before it is logged (default 5.0).",
    )
    parser.add_argument("--log-dir", default="logs", help="Output directory (default logs).")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    E006Logger(args).run()


if __name__ == "__main__":
    main()
