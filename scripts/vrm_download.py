"""Download historical telemetry from the Victron VRM portal.

Pulls VRM statistics for a past time window and writes them to CSV, using a
VRM API access token. Complements the E006 logger by confirming past events.

Reads VICTRON_ACCESS_TOKEN (and optionally VICTRON_SITE_ID) from .env, or via
--token / --site-id. The site is auto-discovered by name otherwise.

Examples:

  python3 scripts/vrm_download.py --from "2026-08-12 03:30" --to "2026-08-12 09:00" --out night.csv
  python3 scripts/vrm_download.py --list
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://vrmapi.victronenergy.com/v2"

VENUS_NAMES = {
    "bs": "battery_soc_pct",
    "ac_loads": "loads_w",
    "consumption": "consumption_w",
    "consumption_input": "input_w",
    "consumption_output": "output_w",
    "solar_yield": "solar_w",
    "hub_inverter": "inverter_w",
    "from_to_grid": "grid_w",
    "ac_out": "ac_out_w",
    "pv_inverter": "pv_inverter_w",
}

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%H:%M:%S",
    "%H:%M",
)


def load_env(env_path: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    path = Path(env_path)
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def resolve_tz(name: str | None):
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def parse_local_time(value: str, tz) -> float:
    text = value.strip()
    for fmt in TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            today = datetime.now(tz)
            parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
        return parsed.replace(tzinfo=tz).timestamp()
    raise ValueError(f"Unrecognised time: {value}")


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")


def list_sites(token: str) -> list[dict]:
    headers = {"X-Authorization": f"Token {token}"}
    resp = requests.get(
        f"{BASE_URL}/users/me",
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    user_id = resp.json()["user"]["id"]
    resp = requests.get(
        f"{BASE_URL}/users/{user_id}/installations",
        headers=headers,
        params={"extended": 1},
        timeout=30,
    )
    resp.raise_for_status()
    return [
        {"idSite": s["idSite"], "name": s.get("name"), "identifier": s.get("identifier")}
        for s in resp.json()["records"]
    ]


def resolve_site(token: str, site_id: int | None, site_name: str | None) -> dict:
    sites = list_sites(token)
    if site_id is not None:
        for s in sites:
            if s["idSite"] == site_id:
                return s
        raise SystemExit(f"site id {site_id} not found")
    if site_name is not None:
        for s in sites:
            if (s.get("name") or "").lower() == site_name.lower():
                return s
        raise SystemExit(f"site named {site_name!r} not found; found: {[s['name'] for s in sites]}")
    if len(sites) == 1:
        return sites[0]
    raise SystemExit(f"multiple sites, pass --site-id: {[(s['idSite'], s['name']) for s in sites]}")


def fetch_stats(token: str, site_id: int, start: int, end: int, interval: str, type_: str, attribute_codes: list[str] | None) -> dict:
    headers = {"X-Authorization": f"Token {token}"}
    params: list[tuple[str, object]] = [
        ("start", start),
        ("end", end),
        ("interval", interval),
        ("type", type_),
    ]
    if attribute_codes:
        params += [("attributeCodes[]", c) for c in attribute_codes]
    resp = requests.get(
        f"{BASE_URL}/installations/{site_id}/stats",
        params=params,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["records"]


def flatten(records: dict, type_: str) -> tuple[list[int], list[str], dict[tuple[int, str], object]]:
    rows: dict[int, dict[str, object]] = {}
    columns: list[str] = []
    seen: set[str] = set()

    def add_col(name: str) -> None:
        if name not in seen:
            seen.add(name)
            columns.append(name)

    for code, series in records.items():
        if not isinstance(series, list):
            continue
        if type_ == "venus":
            base = VENUS_NAMES.get(code, code)
        else:
            base = sanitize(code)
        for pt in series:
            ts = int(pt[0])
            row = rows.setdefault(ts, {})
            if len(pt) >= 4 and pt[2] is not None and pt[3] is not None:
                add_col(base)
                add_col(f"{base}_min")
                add_col(f"{base}_max")
                row[base] = pt[1]
                row[f"{base}_min"] = pt[2]
                row[f"{base}_max"] = pt[3]
            else:
                add_col(base)
                row[base] = pt[1]

    timestamps = sorted(rows)
    return timestamps, columns, {(ts, c): rows[ts].get(c) for ts in timestamps for c in columns}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download historical VRM telemetry to CSV.")
    parser.add_argument("--from", dest="from_time", default=None, help='Window start, e.g. "2026-08-12 03:30".')
    parser.add_argument("--to", dest="to_time", default=None, help='Window end, e.g. "2026-08-12 09:00".')
    parser.add_argument("--tz", default="Europe/Madrid", help="IANA timezone for --from/--to (default Europe/Madrid).")
    parser.add_argument("--interval", default="15mins", help="Interval: minutes|15mins|hours|2hours|days (default 15mins).")
    parser.add_argument("--type", dest="type_", default="venus", help="Stats type: venus|custom|generator|kwh (default venus).")
    parser.add_argument("--attribute-codes", default=None, help="Comma-separated attribute codes (for --type custom).")
    parser.add_argument("--site-id", type=int, default=None, help="Site id (default: auto-discover).")
    parser.add_argument("--site-name", default="Alaro", help="Site name to match when auto-discovering.")
    parser.add_argument("--token", default=None, help="VRM access token (default: VICTRON_ACCESS_TOKEN in .env).")
    parser.add_argument("--out", default=None, help="CSV output path (default: none, table only).")
    parser.add_argument("--list", action="store_true", help="List available sites and exit.")
    parser.add_argument("--env-file", default=".env", help="Path to .env file (default .env).")
    args = parser.parse_args()

    env = load_env(args.env_file)
    token = args.token or env.get("VICTRON_ACCESS_TOKEN")
    if not token:
        print("missing token: pass --token or set VICTRON_ACCESS_TOKEN in .env", file=sys.stderr)
        sys.exit(1)

    if args.list:
        for s in list_sites(token):
            print(f"  {s['idSite']}  {s['name']}  ({s['identifier']})")
        return

    site = resolve_site(token, args.site_id, args.site_name)
    print(f"site: {site['name']} (id {site['idSite']})")

    tz = resolve_tz(args.tz)
    if not args.from_time or not args.to_time:
        print("--from and --to are required", file=sys.stderr)
        sys.exit(1)
    start = int(parse_local_time(args.from_time, tz))
    end = int(parse_local_time(args.to_time, tz))

    codes = [c for c in args.attribute_codes.split(",") if c.strip()] if args.attribute_codes else None
    records = fetch_stats(token, site["idSite"], start, end, args.interval, args.type_, codes)
    timestamps, columns, cells = flatten(records, args.type_)

    if not timestamps:
        print("no data returned for the given window/type/codes", file=sys.stderr)
        sys.exit(1)

    header = ["time", *columns]
    width = max(len(h) for h in header)
    print("  ".join(h.ljust(width) for h in header))
    for ts in timestamps:
        stamp = datetime.fromtimestamp(ts / 1000, tz).strftime("%Y-%m-%d %H:%M")
        vals = [stamp, *[cells.get((ts, c), "") for c in columns]]
        print("  ".join(str(v).ljust(width) if not isinstance(v, str) or len(v) < width else v for v in vals))

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", *columns])
            for ts in timestamps:
                stamp = datetime.fromtimestamp(ts / 1000, tz).strftime("%Y-%m-%d %H:%M")
                writer.writerow([stamp, *[cells.get((ts, c), "") for c in columns]])
        print(f"wrote {args.out} ({len(timestamps)} rows, {len(columns)} columns)")


if __name__ == "__main__":
    main()
