from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cerbo import CerboSettings
from .shelly import ShellySettings


@dataclass(frozen=True)
class Settings:
    email: str | None = None
    password: str | None = None
    site_id: int | None = None
    cerbo_host: str = "192.168.68.84"
    cerbo_port: int = 502
    cerbo_site_name: str = "Alaro (Cerbo GX)"
    cerbo_site_identifier: str = "cerbo-local"
    cerbo_mock_enabled: bool = False
    weather_latitude: float = 39.707337
    weather_longitude: float = 2.791675
    weather_timezone: str = "Europe/Madrid"
    sunshine_hours_min: float = 6.5
    battery_min_soc_percent: float = 55.0
    auto_off_start_local: str = "18:00"
    auto_resume_start_local: str = "08:00"
    auto_control_timezone: str = "Europe/Madrid"
    surplus_night_enabled: bool = True
    surplus_night_base_load_kw: float = 1.5
    surplus_night_hard_min_soc_percent: float = 25.0
    surplus_night_buffer_soc_percent: float = 5.0
    surplus_night_turn_on_margin_soc_percent: float = 10.0
    surplus_night_turn_off_margin_soc_percent: float = 5.0
    surplus_night_next_day_sunshine_min: float = 9.0
    state_file: str = ".state/pump-policy-state.json"
    database_url: str = "sqlite:///.state/automation.db"
    database_auto_migrate: bool = False
    shelly_host: str | None = None
    shelly_port: int = 80
    shelly_switch_id: int = 0
    shelly_username: str | None = None
    shelly_password: str | None = None
    shelly_use_https: bool = False
    shelly_timeout_seconds: float = 5.0
    smtp_gmail_sender: str = "kkotsche1@gmail.com"
    smtp_gmail_app_password: str | None = None
    smtp_gmail_recipients: tuple[str, ...] = (
        "f.kotschenreuther@yahoo.de",
        "monika_kotschenreuther@yahoo.de",
        "kkotsche1@gmail.com",
    )

    def cerbo_settings(self) -> CerboSettings:
        return CerboSettings(
            host=self.cerbo_host,
            port=self.cerbo_port,
            site_name=self.cerbo_site_name,
            site_identifier=self.cerbo_site_identifier,
            site_id=self.site_id or 0,
        )

    def shelly_settings(self) -> ShellySettings:
        if not self.shelly_host:
            raise ValueError("SHELLY_HOST is required for Shelly plug commands.")

        return ShellySettings(
            host=self.shelly_host,
            port=self.shelly_port,
            switch_id=self.shelly_switch_id,
            username=self.shelly_username,
            password=self.shelly_password,
            use_https=self.shelly_use_https,
            timeout_seconds=self.shelly_timeout_seconds,
        )


def load_settings(env_path: str | Path = ".env") -> Settings:
    values: dict[str, str] = {}
    path = Path(env_path)

    if not path.exists():
        raise FileNotFoundError(f"Missing environment file: {path}")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    _reject_removed_keys(values)
    site_id_raw = values.get("VICTRON_SITE_ID")
    site_id = int(site_id_raw) if site_id_raw else None
    weather_timezone = values.get("WEATHER_TIMEZONE", "Europe/Madrid")
    auto_control_timezone = values.get("AUTO_CONTROL_TIMEZONE", weather_timezone)
    _validate_timezone(weather_timezone, key="WEATHER_TIMEZONE")
    _validate_timezone(auto_control_timezone, key="AUTO_CONTROL_TIMEZONE")
    sunshine_hours_min = _parse_hours(
        values.get("SUNSHINE_HOURS_MIN", "6.5"),
        key="SUNSHINE_HOURS_MIN",
    )
    battery_min_soc_percent = _parse_percent(
        values.get("BATTERY_MIN_SOC_PERCENT", "55"),
        key="BATTERY_MIN_SOC_PERCENT",
    )
    surplus_night_hard_min_soc_percent = _parse_percent(
        values.get("SURPLUS_NIGHT_HARD_MIN_SOC_PERCENT", "25"),
        key="SURPLUS_NIGHT_HARD_MIN_SOC_PERCENT",
    )
    surplus_night_buffer_soc_percent = _parse_percent(
        values.get("SURPLUS_NIGHT_BUFFER_SOC_PERCENT", "5"),
        key="SURPLUS_NIGHT_BUFFER_SOC_PERCENT",
    )
    surplus_night_turn_on_margin_soc_percent = _parse_percent(
        values.get("SURPLUS_NIGHT_TURN_ON_MARGIN_SOC_PERCENT", "10"),
        key="SURPLUS_NIGHT_TURN_ON_MARGIN_SOC_PERCENT",
    )
    surplus_night_turn_off_margin_soc_percent = _parse_percent(
        values.get("SURPLUS_NIGHT_TURN_OFF_MARGIN_SOC_PERCENT", "5"),
        key="SURPLUS_NIGHT_TURN_OFF_MARGIN_SOC_PERCENT",
    )
    surplus_night_next_day_sunshine_min = _parse_hours(
        values.get("SURPLUS_NIGHT_NEXT_DAY_SUNSHINE_MIN", "9.0"),
        key="SURPLUS_NIGHT_NEXT_DAY_SUNSHINE_MIN",
    )
    surplus_night_base_load_kw = _parse_non_negative_float(
        values.get("SURPLUS_NIGHT_BASE_LOAD_KW", "1.5"),
        key="SURPLUS_NIGHT_BASE_LOAD_KW",
    )

    return Settings(
        email=values.get("VICTRON_EMAIL"),
        password=values.get("VICTRON_PASSWORD"),
        site_id=site_id,
        cerbo_host=values.get("CERBO_HOST", "192.168.68.66"),
        cerbo_port=int(values.get("CERBO_PORT", "502")),
        cerbo_site_name=values.get("CERBO_SITE_NAME", "Alaro (Cerbo GX)"),
        cerbo_site_identifier=values.get("CERBO_SITE_IDENTIFIER", "cerbo-local"),
        cerbo_mock_enabled=values.get("CERBO_MOCK_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        },
        weather_latitude=float(values.get("WEATHER_LATITUDE", "39.707337")),
        weather_longitude=float(values.get("WEATHER_LONGITUDE", "2.791675")),
        weather_timezone=weather_timezone,
        sunshine_hours_min=sunshine_hours_min,
        battery_min_soc_percent=battery_min_soc_percent,
        auto_off_start_local=_parse_local_hhmm(
            values.get("AUTO_OFF_START_LOCAL", "18:00"),
            key="AUTO_OFF_START_LOCAL",
        ),
        auto_resume_start_local=_parse_local_hhmm(
            values.get("AUTO_RESUME_START_LOCAL", "08:00"),
            key="AUTO_RESUME_START_LOCAL",
        ),
        auto_control_timezone=auto_control_timezone,
        surplus_night_enabled=values.get("SURPLUS_NIGHT_ENABLED", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        },
        surplus_night_base_load_kw=surplus_night_base_load_kw,
        surplus_night_hard_min_soc_percent=surplus_night_hard_min_soc_percent,
        surplus_night_buffer_soc_percent=surplus_night_buffer_soc_percent,
        surplus_night_turn_on_margin_soc_percent=surplus_night_turn_on_margin_soc_percent,
        surplus_night_turn_off_margin_soc_percent=surplus_night_turn_off_margin_soc_percent,
        surplus_night_next_day_sunshine_min=surplus_night_next_day_sunshine_min,
        state_file=values.get("PUMP_POLICY_STATE_FILE", ".state/pump-policy-state.json"),
        database_url=values.get("DATABASE_URL", "sqlite:///.state/automation.db"),
        database_auto_migrate=values.get("DATABASE_AUTO_MIGRATE", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        },
        shelly_host=values.get("SHELLY_HOST"),
        shelly_port=int(values.get("SHELLY_PORT", "80")),
        shelly_switch_id=int(values.get("SHELLY_SWITCH_ID", "0")),
        shelly_username=values.get("SHELLY_USERNAME"),
        shelly_password=values.get("SHELLY_PASSWORD"),
        shelly_use_https=values.get("SHELLY_USE_HTTPS", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        },
        shelly_timeout_seconds=float(values.get("SHELLY_TIMEOUT_SECONDS", "5.0")),
        smtp_gmail_sender=values.get("SMTP_GMAIL_SENDER", "kkotsche1@gmail.com"),
        smtp_gmail_app_password=values.get("SMTP_GMAIL_APP_PASSWORD"),
        smtp_gmail_recipients=_parse_csv_list(
            values.get(
                "SMTP_GMAIL_RECIPIENTS",
                ",".join(
                    (
                        "f.kotschenreuther@yahoo.de",
                        "monika_kotschenreuther@yahoo.de",
                        "kkotsche1@gmail.com",
                    )
                ),
            )
        ),
    )


def _parse_local_hhmm(value: str, *, key: str) -> str:
    candidate = value.strip()
    if len(candidate) != 5 or candidate[2] != ":":
        raise ValueError(f"{key} must use HH:MM 24-hour format.")
    hour_raw, minute_raw = candidate.split(":", 1)
    if not (hour_raw.isdigit() and minute_raw.isdigit()):
        raise ValueError(f"{key} must use HH:MM 24-hour format.")
    hour = int(hour_raw)
    minute = int(minute_raw)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"{key} must be a valid 24-hour clock time.")
    return f"{hour:02d}:{minute:02d}"


def _parse_percent(value: str, *, key: str) -> float:
    try:
        candidate = float(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a numeric percentage.") from exc
    if candidate < 0 or candidate > 100:
        raise ValueError(f"{key} must be between 0 and 100.")
    return candidate


def _parse_hours(value: str, *, key: str) -> float:
    try:
        candidate = float(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a numeric hour value.") from exc
    if candidate < 0 or candidate > 24:
        raise ValueError(f"{key} must be between 0 and 24.")
    return candidate


def _parse_non_negative_float(value: str, *, key: str) -> float:
    try:
        candidate = float(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a numeric value.") from exc
    if candidate < 0:
        raise ValueError(f"{key} must be greater than or equal to zero.")
    return candidate


def _reject_removed_keys(values: dict[str, str]) -> None:
    removed_keys = (
        "BATTERY_OFF_BELOW_SOC_PERCENT",
        "BATTERY_RESUME_ABOVE_SOC_PERCENT",
        "SUMMER_START_MONTH_DAY",
        "WINTER_START_MONTH_DAY",
        "SUMMER_AUTO_OFF_START_LOCAL",
        "SUMMER_AUTO_RESUME_START_LOCAL",
        "WINTER_AUTO_OFF_START_LOCAL",
        "WINTER_AUTO_RESUME_START_LOCAL",
    )
    present = [key for key in removed_keys if key in values]
    if present:
        if any(key.startswith(("SUMMER_", "WINTER_")) for key in present):
            seasonal_keys = ", ".join(sorted(present))
            raise ValueError(
                "Seasonal quiet-hours settings are no longer supported. Remove "
                f"{seasonal_keys} and use AUTO_OFF_START_LOCAL plus "
                "AUTO_RESUME_START_LOCAL year-round."
            )
        raise ValueError(
            "Deprecated battery hysteresis settings are no longer supported. Replace "
            f"{', '.join(present)} with BATTERY_MIN_SOC_PERCENT."
        )


def _validate_timezone(value: str, *, key: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"{key} must be a valid IANA timezone name.") from exc


def _parse_csv_list(value: str) -> tuple[str, ...]:
    entries = tuple(part.strip() for part in value.split(",") if part.strip())
    if entries:
        return entries
    return (
        "f.kotschenreuther@yahoo.de",
        "monika_kotschenreuther@yahoo.de",
        "kkotsche1@gmail.com",
    )
