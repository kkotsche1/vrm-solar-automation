"""Read back the E006 logger's daily JSONL.gz files.

Replays the captured telemetry into a one-row-per-second CSV (values
forward-filled, since MQTT only sends changes) for the time window around a
failure, or prints a plain summary of state changes for a whole night.

Usage:

    python3 analyse_window.py logs/cerbo-2026-08-13.jsonl.gz \
        --from "2026-08-14 04:20" --to "2026-08-14 05:30"

    python3 analyse_window.py logs/cerbo-2026-08-13.jsonl.gz --summary
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_LABELS = {
    0: "UNKNOWN",
    1: "GRID",
    2: "GENERATOR",
    3: "SHORE",
    240: "NOT CONNECTED",
}

SIGNAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^system/\d+/Ac/ActiveIn/Source$"), "source"),
    (re.compile(r"^vebus/\d+/Ac/ActiveIn/L(\d)/V$"), "in_V{n}"),
    (re.compile(r"^vebus/\d+/Ac/ActiveIn/L(\d)/F$"), "in_F{n}"),
    (re.compile(r"^vebus/\d+/Ac/ActiveIn/L(\d)/I$"), "in_I{n}"),
    (re.compile(r"^vebus/\d+/Ac/ActiveIn/L(\d)/P$"), "in_P{n}"),
    (re.compile(r"^vebus/\d+/Ac/ActiveIn/L(\d)/S$"), "in_S{n}"),
    (re.compile(r"^vebus/\d+/Ac/ActiveIn/CurrentLimit$"), "limit_A"),
    (re.compile(r"^vebus/\d+/Ac/ActiveIn/Connected$"), "active_in_connected"),
    (re.compile(r"^vebus/\d+/Ac/ActiveIn/ActiveInput$"), "active_input"),
    (re.compile(r"^vebus/\d+/Ac/Out/L(\d)/V$"), "out_V{n}"),
    (re.compile(r"^vebus/\d+/Ac/Out/L(\d)/F$"), "out_F{n}"),
    (re.compile(r"^vebus/\d+/Ac/Out/L(\d)/I$"), "out_I{n}"),
    (re.compile(r"^vebus/\d+/Ac/Out/L(\d)/P$"), "out_P{n}"),
    (re.compile(r"^vebus/\d+/Ac/Out/L(\d)/S$"), "out_S{n}"),
    (re.compile(r"^vebus/\d+/AcSensor/(\d+)/Power$"), "acsensor_P{n}"),
    (re.compile(r"^vebus/\d+/AcSensor/(\d+)/Current$"), "acsensor_I{n}"),
    (re.compile(r"^vebus/\d+/AcSensor/(\d+)/Voltage$"), "acsensor_V{n}"),
    (re.compile(r"^vebus/\d+/AcSensor/(\d+)/Phase$"), "acsensor_phase{n}"),
    (re.compile(r"^vebus/\d+/State$"), "vebus_state"),
    (re.compile(r"^vebus/\d+/Mode$"), "vebus_mode"),
    (re.compile(r"^vebus/\d+/VebusError$"), "vebus_error"),
    (re.compile(r"^vebus/\d+/VebusMainState$"), "vebus_main_state"),
    (re.compile(r"^vebus/\d+/Dc/0/Voltage$"), "vebus_dc_voltage"),
    (re.compile(r"^vebus/\d+/Dc/0/Current$"), "vebus_dc_current"),
    (re.compile(r"^vebus/\d+/Dc/0/Power$"), "vebus_dc_power"),
    (re.compile(r"^vebus/\d+/Alarms/GridLost$"), "alarm_grid_lost"),
    (re.compile(r"^vebus/\d+/Alarms/PhaseRotation$"), "alarm_phase_rotation"),
    (re.compile(r"^vebus/\d+/Alarms/Overload$"), "alarm_overload"),
    (re.compile(r"^vebus/\d+/Alarms/Ripple$"), "alarm_ripple"),
    (re.compile(r"^generator/\d+/Generator0/State$"), "genset_state"),
    (re.compile(r"^generator/\d+/Generator0/Error$"), "genset_error"),
    (re.compile(r"^generator/\d+/Generator0/RunningByCondition$"), "genset_run_reason"),
    (re.compile(r"^generator/\d+/Generator0/RunningByConditionCode$"), "genset_run_reason_code"),
    (re.compile(r"^generator/\d+/Generator0/ManualStart$"), "genset_manual_start"),
    (re.compile(r"^generator/\d+/Generator0/Alarms/NoGeneratorAtAcIn$"), "alarm_no_generator_at_ac_in"),
    (re.compile(r"^generator/\d+/Generator0/Runtime$"), "genset_runtime"),
    (re.compile(r"^generator/\d+/Generator0/TodayRuntime$"), "genset_today_runtime"),
    (re.compile(r"^system/\d+/Ac/Consumption/L(\d)/Power$"), "cons_L{n}"),
    (re.compile(r"^system/\d+/Ac/Consumption/L(\d)/Current$"), "cons_I{n}"),
    (re.compile(r"^system/\d+/Ac/ConsumptionOnInput/L(\d)/Power$"), "cons_on_input_L{n}"),
    (re.compile(r"^system/\d+/Ac/ConsumptionOnOutput/L(\d)/Power$"), "cons_on_output_L{n}"),
    (re.compile(r"^system/\d+/Ac/Genset/L(\d)/Power$"), "genset_L{n}"),
    (re.compile(r"^system/\d+/Ac/Grid/L(\d)/Power$"), "grid_L{n}"),
    (re.compile(r"^system/\d+/Dc/Battery/Soc$"), "soc"),
    (re.compile(r"^system/\d+/Dc/Battery/Power$"), "battery_power"),
    (re.compile(r"^system/\d+/Dc/Battery/Voltage$"), "battery_voltage"),
    (re.compile(r"^system/\d+/Dc/Battery/Current$"), "battery_current"),
    (re.compile(r"^system/\d+/Dc/Battery/Temperature$"), "battery_temp"),
    (re.compile(r"^system/\d+/Dc/Pv/Power$"), "solar_power"),
    (re.compile(r"^heatpump/\d+/Ac/Power$"), "heatpump_power"),
    (re.compile(r"^heatpump/\d+/Ac/L(\d)/Power$"), "heatpump_P{n}"),
    (re.compile(r"^heatpump/\d+/Ac/L(\d)/Voltage$"), "heatpump_V{n}"),
]

SHELLY_COLUMNS = ("shelly_voltage", "shelly_apower", "shelly_current", "shelly_output", "shelly_temp")


def strip_prefix(topic: str) -> str:
    parts = topic.split("/")
    if parts and parts[0] == "N":
        return "/".join(parts[2:])
    return topic


def resolve_signal(topic: str) -> str | None:
    path = strip_prefix(topic)
    for pattern, template in SIGNAL_PATTERNS:
        match = pattern.match(path)
        if match:
            if "{n}" in template:
                return template.replace("{n}", match.group(1))
            return template
    return None


def sanitize(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", path).strip("_")


def load_events(paths: list[str], include_all: bool) -> tuple[list[tuple[int, str, object]], list[str]]:
    events: list[tuple[int, str, object]] = []
    columns: list[str] = []
    seen: set[str] = set()

    def add_column(name: str) -> None:
        if name not in seen:
            seen.add(name)
            columns.append(name)

    for path in paths:
        with gzip.open(path, "rb") as fh:
            while True:
                try:
                    raw_line = fh.readline()
                except EOFError:
                    break
                if not raw_line:
                    break
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                t = record.get("t")
                if not isinstance(t, (int, float)):
                    continue
                sec = int(t)
                if "shelly" in record:
                    shelly = record["shelly"]
                    for key, column in (
                        ("voltage", "shelly_voltage"),
                        ("apower", "shelly_apower"),
                        ("current", "shelly_current"),
                        ("output", "shelly_output"),
                        ("temp", "shelly_temp"),
                    ):
                        if key in shelly and shelly[key] is not None:
                            add_column(column)
                            events.append((sec, column, shelly[key]))
                    continue
                if "comap" in record:
                    comap = record["comap"]
                    for key, value in comap.items():
                        if value is None:
                            continue
                        column = f"comap_{key}"
                        add_column(column)
                        events.append((sec, column, value))
                    continue
                topic = record.get("topic")
                value = record.get("v")
                if not isinstance(topic, str):
                    continue
                signal = resolve_signal(topic)
                if signal is not None:
                    add_column(signal)
                    events.append((sec, signal, value))
                elif include_all and _is_numeric(value):
                    column = sanitize(strip_prefix(topic))
                    add_column(column)
                    events.append((sec, column, value))

    events.sort(key=lambda e: (e[0], e[1]))
    return events, columns


def _is_numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def resolve_tz(name: str | None):
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def parse_local_time(value: str, tz) -> float:
    text = value.strip()
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%H:%M:%S",
        "%H:%M",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            today = datetime.now(tz)
            parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
        return parsed.replace(tzinfo=tz).timestamp()
    raise ValueError(f"Unrecognised time: {value}")


def build_rows(
    events: list[tuple[int, str, object]],
    columns: list[str],
    from_t: float,
    to_t: float,
) -> list[tuple[int, dict[str, object]]]:
    current: dict[str, object] = {}
    idx = 0
    while idx < len(events) and events[idx][0] < int(from_t):
        current[events[idx][1]] = events[idx][2]
        idx += 1

    rows: list[tuple[int, dict[str, object]]] = []
    for sec in range(int(from_t), int(to_t) + 1):
        while idx < len(events) and events[idx][0] <= sec:
            current[events[idx][1]] = events[idx][2]
            idx += 1
        rows.append((sec, dict(current)))
    return rows


def write_csv(rows: list[tuple[int, dict[str, object]]], columns: list[str], out: str, tz) -> None:
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "epoch", *columns])
        for sec, values in rows:
            stamp = datetime.fromtimestamp(sec, tz).strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([stamp, sec, *[values.get(c, "") for c in columns]])
    print(f"wrote {out} ({len(rows)} rows, {len(columns)} signals)")


def print_summary(rows: list[tuple[int, dict[str, object]]], tz, args) -> None:
    last_source = None
    hp_running = False
    hp_low = None
    hp_high = None
    on_step = args.hp_on_step_w
    off_step = args.hp_off_step_w

    for sec, values in rows:
        source = values.get("source")
        if _is_numeric(source):
            source = int(source)
            if last_source is None:
                last_source = source
                stamp = datetime.fromtimestamp(sec, tz).strftime("%Y-%m-%d %H:%M:%S")
                print(f"{stamp}  AC source -> {SOURCE_LABELS.get(source, source)} (source={source})")
            elif source != last_source:
                stamp = datetime.fromtimestamp(sec, tz).strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"{stamp}  AC source changed {SOURCE_LABELS.get(last_source, last_source)} "
                    f"-> {SOURCE_LABELS.get(source, source)} (source={source})"
                )
                last_source = source

        total = _total_consumption(values)
        if total is None:
            continue
        if not hp_running:
            if hp_low is None or total < hp_low:
                hp_low = total
            if hp_low is not None and total >= hp_low + on_step:
                hp_running = True
                hp_high = total
                stamp = datetime.fromtimestamp(sec, tz).strftime("%Y-%m-%d %H:%M:%S")
                print(f"{stamp}  HEAT PUMP STARTED (total load {total:.0f} W)")
        else:
            if hp_high is None or total > hp_high:
                hp_high = total
            if hp_high is not None and total <= hp_high - off_step:
                hp_running = False
                hp_low = total
                stamp = datetime.fromtimestamp(sec, tz).strftime("%Y-%m-%d %H:%M:%S")
                print(f"{stamp}  HEAT PUMP STOPPED (total load {total:.0f} W)")


def _total_consumption(values: dict[str, object]) -> float | None:
    known = [v for k, v in values.items() if k.startswith("cons_L") and _is_numeric(v)]
    return sum(known) if known else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay E006 logger captures as CSV or a summary.")
    parser.add_argument("logs", nargs="+", help="One or more cerbo-*.jsonl.gz files.")
    parser.add_argument("--from", dest="from_time", default=None, help='Window start, e.g. "2026-08-14 04:20".')
    parser.add_argument("--to", dest="to_time", default=None, help='Window end, e.g. "2026-08-14 05:30".')
    parser.add_argument("--out", default="window.csv", help="CSV output path (default window.csv).")
    parser.add_argument("--tz", default=None, help="IANA timezone for --from/--to (default: system local).")
    parser.add_argument("--summary", action="store_true", help="Print state changes instead of writing CSV.")
    parser.add_argument(
        "--all-topics",
        action="store_true",
        help="Emit a column for every numeric topic, not just the curated signals.",
    )
    parser.add_argument(
        "--hp-on-step-w", type=float, default=900.0, help="Heat-pump start step (W), summary mode."
    )
    parser.add_argument(
        "--hp-off-step-w", type=float, default=900.0, help="Heat-pump stop step (W), summary mode."
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    for path in args.logs:
        if not Path(path).exists():
            print(f"missing file: {path}", file=sys.stderr)
            sys.exit(1)

    tz = resolve_tz(args.tz)
    events, columns = load_events(args.logs, args.all_topics)
    if not events:
        print("no telemetry found in the given files", file=sys.stderr)
        sys.exit(1)

    if args.from_time:
        from_t = parse_local_time(args.from_time, tz)
    else:
        from_t = float(events[0][0])
    if args.to_time:
        to_t = parse_local_time(args.to_time, tz)
    else:
        to_t = float(events[-1][0])

    if to_t < from_t:
        print("--to must be later than --from", file=sys.stderr)
        sys.exit(1)

    rows = build_rows(events, columns, from_t, to_t)

    if args.summary:
        print_summary(rows, tz, args)
        return

    write_csv(rows, columns, args.out, tz)


if __name__ == "__main__":
    main()
