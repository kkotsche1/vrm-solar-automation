from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import random
from pathlib import Path
from typing import Any

import aiohttp

from .config import load_settings

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

HEADER_ROW_COUNT = 3

WEATHER_COLUMN_LABELS = (
    "Weather local date",
    "Weather min temp",
    "Weather max temp",
    "Weather mean temp",
    "Weather sunshine hours",
    "Weather code",
)


@dataclass(frozen=True)
class DailyWeather:
    local_date: date
    min_temperature_c: float | None
    max_temperature_c: float | None
    mean_temperature_c: float | None
    sunshine_hours: float | None
    weather_code: int | None

    def as_csv_values(self) -> list[str]:
        return [
            self.local_date.isoformat(),
            _to_csv_float(self.min_temperature_c),
            _to_csv_float(self.max_temperature_c),
            _to_csv_float(self.mean_temperature_c),
            _to_csv_float(self.sunshine_hours),
            "" if self.weather_code is None else str(self.weather_code),
        ]


@dataclass(frozen=True)
class AugmentResult:
    input_path: Path
    output_path: Path
    total_rows: int
    weather_enriched_rows: int
    missing_weather_rows: int
    queried_date_count: int
    requested_ranges: int


class HistoricalWeatherClient:
    async def fetch_daily_weather(
        self,
        *,
        session: aiohttp.ClientSession,
        latitude: float,
        longitude: float,
        timezone: str,
        start_date: date,
        end_date: date,
        max_attempts: int = 8,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
    ) -> dict[date, DailyWeather]:
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "daily": (
                "temperature_2m_min,temperature_2m_max,temperature_2m_mean,"
                "weather_code,sunshine_duration"
            ),
        }

        for attempt in range(1, max_attempts + 1):
            try:
                async with session.get(ARCHIVE_URL, params=params) as response:
                    if response.status in {429, 500, 502, 503, 504}:
                        if attempt == max_attempts:
                            response.raise_for_status()
                        delay_seconds = _compute_retry_delay_seconds(
                            response=response,
                            attempt=attempt,
                            base_backoff_seconds=base_backoff_seconds,
                            max_backoff_seconds=max_backoff_seconds,
                        )
                        await asyncio.sleep(delay_seconds)
                        continue

                    response.raise_for_status()
                    payload = await response.json()
                    return _parse_daily_weather_payload(payload)
            except (aiohttp.ClientError, TimeoutError):
                if attempt == max_attempts:
                    raise
                delay_seconds = min(max_backoff_seconds, base_backoff_seconds * (2 ** (attempt - 1)))
                delay_seconds += random.uniform(0.0, 0.25)
                await asyncio.sleep(delay_seconds)

        return {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Augment historical CSV data with Open-Meteo archive weather columns.",
    )
    parser.add_argument(
        "--historicals-dir",
        default="historicals",
        help="Directory containing the historical CSV files.",
    )
    parser.add_argument(
        "--glob",
        default="*.csv",
        help="Glob pattern used to select files inside --historicals-dir.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Environment file for default weather coordinates/timezone.",
    )
    parser.add_argument(
        "--latitude",
        type=float,
        default=None,
        help="Override WEATHER_LATITUDE from the env file.",
    )
    parser.add_argument(
        "--longitude",
        type=float,
        default=None,
        help="Override WEATHER_LONGITUDE from the env file.",
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="Override WEATHER_TIMEZONE from the env file.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Output directory for augmented files. Defaults to <historicals-dir>/weather_augmented. "
            "Ignored when --in-place is set."
        ),
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite each source CSV in place.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-augment files even when weather columns already exist.",
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=31,
        help="Number of days per Open-Meteo archive request.",
    )
    return parser.parse_args(argv)


async def run_augmentation(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = None
    try:
        settings = load_settings(args.env_file)
    except FileNotFoundError:
        if args.latitude is None or args.longitude is None or args.timezone is None:
            raise

    latitude = settings.weather_latitude if args.latitude is None and settings is not None else args.latitude
    longitude = settings.weather_longitude if args.longitude is None and settings is not None else args.longitude
    timezone = settings.weather_timezone if args.timezone is None and settings is not None else args.timezone
    if latitude is None or longitude is None or timezone is None:
        raise ValueError(
            "Weather coordinates/timezone are required. Provide --env-file with WEATHER_* "
            "or pass --latitude --longitude --timezone."
        )

    input_dir = Path(args.historicals_dir).resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"Historical directory does not exist: {input_dir}")

    file_paths = sorted(path for path in input_dir.glob(args.glob) if path.is_file())
    if not file_paths:
        print(f"No files matched pattern '{args.glob}' in {input_dir}")
        return 0

    output_dir = (
        input_dir / "weather_augmented"
        if args.output_dir is None
        else Path(args.output_dir).resolve()
    )
    if not args.in_place:
        output_dir.mkdir(parents=True, exist_ok=True)

    client = HistoricalWeatherClient()
    async with aiohttp.ClientSession() as session:
        weather_cache: dict[date, DailyWeather] = {}
        total_ranges = 0
        results: list[AugmentResult] = []
        for path in file_paths:
            result = await augment_file_with_weather(
                input_path=path,
                output_path=path if args.in_place else output_dir / path.name,
                session=session,
                client=client,
                latitude=latitude,
                longitude=longitude,
                timezone=timezone,
                weather_cache=weather_cache,
                force=args.force,
                chunk_days=args.chunk_days,
            )
            total_ranges += result.requested_ranges
            results.append(result)

    total_rows = sum(item.total_rows for item in results)
    enriched_rows = sum(item.weather_enriched_rows for item in results)
    missing_rows = sum(item.missing_weather_rows for item in results)
    print(
        "Augmented files: "
        f"{len(results)} | rows: {total_rows} | weather matched: {enriched_rows} | "
        f"weather missing: {missing_rows} | API ranges requested: {total_ranges}"
    )
    for result in results:
        print(f"- {result.input_path.name} -> {result.output_path}")

    return 0


async def augment_file_with_weather(
    *,
    input_path: Path,
    output_path: Path,
    session: aiohttp.ClientSession,
    client: HistoricalWeatherClient,
    latitude: float,
    longitude: float,
    timezone: str,
    weather_cache: dict[date, DailyWeather],
    force: bool,
    chunk_days: int,
) -> AugmentResult:
    rows = _read_csv_rows(input_path)
    if len(rows) < HEADER_ROW_COUNT + 1:
        raise ValueError(f"{input_path} does not contain the expected three header rows and data.")

    header_rows = [list(row) for row in rows[:HEADER_ROW_COUNT]]
    data_rows = [list(row) for row in rows[HEADER_ROW_COUNT:]]

    if not force and any(label in header_rows[1] for label in WEATHER_COLUMN_LABELS):
        return AugmentResult(
            input_path=input_path,
            output_path=output_path,
            total_rows=len(data_rows),
            weather_enriched_rows=0,
            missing_weather_rows=0,
            queried_date_count=0,
            requested_ranges=0,
        )

    timestamp_column = _find_timestamp_column(header_rows[0])
    unique_dates = _collect_unique_dates(data_rows, timestamp_column=timestamp_column)

    requested_ranges = 0
    if unique_dates:
        missing_dates = [entry for entry in unique_dates if entry not in weather_cache]
        if missing_dates:
            for chunk_start, chunk_end in _chunk_date_ranges(missing_dates, chunk_days=max(chunk_days, 1)):
                weather_chunk = await client.fetch_daily_weather(
                    session=session,
                    latitude=latitude,
                    longitude=longitude,
                    timezone=timezone,
                    start_date=chunk_start,
                    end_date=chunk_end,
                )
                weather_cache.update(weather_chunk)
                requested_ranges += 1

    augmented_rows, weather_enriched_rows, missing_weather_rows = _augment_rows_with_weather(
        header_rows=header_rows,
        data_rows=data_rows,
        timestamp_column=timestamp_column,
        weather_by_date=weather_cache,
    )
    _write_csv_rows(output_path, augmented_rows)

    return AugmentResult(
        input_path=input_path,
        output_path=output_path,
        total_rows=len(data_rows),
        weather_enriched_rows=weather_enriched_rows,
        missing_weather_rows=missing_weather_rows,
        queried_date_count=len(unique_dates),
        requested_ranges=requested_ranges,
    )


def _augment_rows_with_weather(
    *,
    header_rows: list[list[str]],
    data_rows: list[list[str]],
    timestamp_column: int,
    weather_by_date: dict[date, DailyWeather],
) -> tuple[list[list[str]], int, int]:
    header_one = list(header_rows[0]) + ["Weather [Open-Meteo Archive]"] * len(WEATHER_COLUMN_LABELS)
    header_two = list(header_rows[1]) + list(WEATHER_COLUMN_LABELS)
    header_three = list(header_rows[2]) + ["", "C", "C", "C", "h", ""]

    weather_enriched_rows = 0
    missing_weather_rows = 0
    augmented_data_rows: list[list[str]] = []
    for row in data_rows:
        timestamp_raw = _safe_cell(row, timestamp_column)
        if not timestamp_raw:
            augmented_data_rows.append(list(row) + [""] * len(WEATHER_COLUMN_LABELS))
            missing_weather_rows += 1
            continue

        row_date = datetime.fromisoformat(timestamp_raw).date()
        daily_weather = weather_by_date.get(row_date)
        if daily_weather is None:
            augmented_data_rows.append(list(row) + [""] * len(WEATHER_COLUMN_LABELS))
            missing_weather_rows += 1
            continue

        augmented_data_rows.append(list(row) + daily_weather.as_csv_values())
        weather_enriched_rows += 1

    return [header_one, header_two, header_three, *augmented_data_rows], weather_enriched_rows, missing_weather_rows


def _parse_daily_weather_payload(payload: dict[str, Any]) -> dict[date, DailyWeather]:
    daily = payload.get("daily", {})
    raw_dates = list(daily.get("time") or [])
    raw_min = list(daily.get("temperature_2m_min") or [])
    raw_max = list(daily.get("temperature_2m_max") or [])
    raw_mean = list(daily.get("temperature_2m_mean") or [])
    raw_sun = list(daily.get("sunshine_duration") or [])
    raw_code = list(daily.get("weather_code") or [])

    result: dict[date, DailyWeather] = {}
    for index, day_raw in enumerate(raw_dates):
        local_date = date.fromisoformat(str(day_raw))
        sunshine_seconds = _optional_float(_value_at(raw_sun, index))
        result[local_date] = DailyWeather(
            local_date=local_date,
            min_temperature_c=_optional_float(_value_at(raw_min, index)),
            max_temperature_c=_optional_float(_value_at(raw_max, index)),
            mean_temperature_c=_optional_float(_value_at(raw_mean, index)),
            sunshine_hours=(sunshine_seconds / 3600.0) if sunshine_seconds is not None else None,
            weather_code=_optional_int(_value_at(raw_code, index)),
        )
    return result


def _find_timestamp_column(header_row: list[str]) -> int:
    for index, value in enumerate(header_row):
        if value.strip().lower() == "timestamp":
            return index
    return 0


def _collect_unique_dates(rows: list[list[str]], *, timestamp_column: int) -> list[date]:
    unique_dates: set[date] = set()
    for row in rows:
        raw_timestamp = _safe_cell(row, timestamp_column)
        if not raw_timestamp:
            continue
        unique_dates.add(datetime.fromisoformat(raw_timestamp).date())
    return sorted(unique_dates)


def _chunk_date_ranges(values: list[date], *, chunk_days: int) -> list[tuple[date, date]]:
    if not values:
        return []
    sorted_values = sorted(values)
    chunks: list[tuple[date, date]] = []
    index = 0
    while index < len(sorted_values):
        start = sorted_values[index]
        end = start + timedelta(days=chunk_days - 1)
        while index + 1 < len(sorted_values) and sorted_values[index + 1] <= end:
            index += 1
        chunks.append((start, sorted_values[index]))
        index += 1
    return chunks


def _compute_retry_delay_seconds(
    *,
    response: aiohttp.ClientResponse,
    attempt: int,
    base_backoff_seconds: float,
    max_backoff_seconds: float,
) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            header_delay = float(retry_after)
        except ValueError:
            header_delay = 0.0
    else:
        header_delay = 0.0

    exponential_delay = min(max_backoff_seconds, base_backoff_seconds * (2 ** (attempt - 1)))
    candidate = max(header_delay, exponential_delay)
    return candidate + random.uniform(0.0, 0.25)


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file_handle:
        return list(csv.reader(file_handle))


def _write_csv_rows(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_handle:
        writer = csv.writer(file_handle)
        writer.writerows(rows)


def _safe_cell(row: list[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return row[index].strip()


def _to_csv_float(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.3f}"


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _value_at(values: list[object], index: int) -> object:
    if index < 0 or index >= len(values):
        return None
    return values[index]


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run_augmentation(argv))
