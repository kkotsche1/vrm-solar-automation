from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
    cerbo_mqtt_enabled: bool = False
    cerbo_mqtt_host: str | None = None
    cerbo_mqtt_port: int = 1883
    cerbo_mqtt_username: str | None = None
    cerbo_mqtt_password: str | None = None
    weather_latitude: float = 39.707337
    weather_longitude: float = 2.791675
    weather_timezone: str = "Europe/Madrid"
    control_interval_seconds: float = 60.0
    telemetry_stale_after_seconds: float = 90.0
    modbus_fallback_poll_seconds: float = 30.0
    policy_debounce_ms: int = 500
    policy_min_run_interval_seconds: float = 5.0
    weather_refresh_seconds: float = 900.0
    state_file: str = ".state/pump-policy-state.json"
    database_file: str = ".state/metrics.db"
    shelly_host: str | None = None
    shelly_port: int = 80
    shelly_switch_id: int = 0
    shelly_username: str | None = None
    shelly_password: str | None = None
    shelly_use_https: bool = False
    shelly_timeout_seconds: float = 5.0

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

    site_id_raw = values.get("VICTRON_SITE_ID")
    site_id = int(site_id_raw) if site_id_raw else None
    cerbo_host = values.get("CERBO_HOST", "192.168.68.66")
    cerbo_port = int(values.get("CERBO_PORT", "502"))
    cerbo_site_name = values.get("CERBO_SITE_NAME", "Alaro (Cerbo GX)")
    cerbo_site_identifier = values.get("CERBO_SITE_IDENTIFIER", "cerbo-local")
    cerbo_mock_enabled = values.get("CERBO_MOCK_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cerbo_mqtt_enabled = values.get("CERBO_MQTT_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cerbo_mqtt_host = values.get("CERBO_MQTT_HOST", cerbo_host)
    cerbo_mqtt_port = int(values.get("CERBO_MQTT_PORT", "1883"))
    telemetry_stale_after_seconds = float(values.get("TELEMETRY_STALE_AFTER_SECONDS", "90.0"))
    modbus_fallback_poll_seconds = float(values.get("MODBUS_FALLBACK_POLL_SECONDS", "30.0"))
    policy_debounce_ms = int(values.get("POLICY_DEBOUNCE_MS", "500"))
    policy_min_run_interval_seconds = float(values.get("POLICY_MIN_RUN_INTERVAL_SECONDS", "5.0"))
    weather_refresh_seconds = float(values.get("WEATHER_REFRESH_SECONDS", "900.0"))

    weather_latitude = float(values.get("WEATHER_LATITUDE", "39.707337"))
    weather_longitude = float(values.get("WEATHER_LONGITUDE", "2.791675"))
    weather_timezone = values.get("WEATHER_TIMEZONE", "Europe/Madrid")
    control_interval_seconds = float(values.get("CONTROL_INTERVAL_SECONDS", "30.0"))
    state_file = values.get("PUMP_POLICY_STATE_FILE", ".state/pump-policy-state.json")
    database_file = values.get("DATABASE_FILE", ".state/metrics.db")
    shelly_port = int(values.get("SHELLY_PORT", "80"))
    shelly_switch_id = int(values.get("SHELLY_SWITCH_ID", "0"))
    shelly_timeout_seconds = float(values.get("SHELLY_TIMEOUT_SECONDS", "5.0"))
    shelly_use_https = values.get("SHELLY_USE_HTTPS", "false").lower() in {"1", "true", "yes", "on"}

    return Settings(
        email=values.get("VICTRON_EMAIL"),
        password=values.get("VICTRON_PASSWORD"),
        site_id=site_id,
        cerbo_host=cerbo_host,
        cerbo_port=cerbo_port,
        cerbo_site_name=cerbo_site_name,
        cerbo_site_identifier=cerbo_site_identifier,
        cerbo_mock_enabled=cerbo_mock_enabled,
        cerbo_mqtt_enabled=cerbo_mqtt_enabled,
        cerbo_mqtt_host=cerbo_mqtt_host,
        cerbo_mqtt_port=cerbo_mqtt_port,
        cerbo_mqtt_username=values.get("CERBO_MQTT_USERNAME"),
        cerbo_mqtt_password=values.get("CERBO_MQTT_PASSWORD"),
        weather_latitude=weather_latitude,
        weather_longitude=weather_longitude,
        weather_timezone=weather_timezone,
        control_interval_seconds=control_interval_seconds,
        telemetry_stale_after_seconds=telemetry_stale_after_seconds,
        modbus_fallback_poll_seconds=modbus_fallback_poll_seconds,
        policy_debounce_ms=policy_debounce_ms,
        policy_min_run_interval_seconds=policy_min_run_interval_seconds,
        weather_refresh_seconds=weather_refresh_seconds,
        state_file=state_file,
        database_file=database_file,
        shelly_host=values.get("SHELLY_HOST"),
        shelly_port=shelly_port,
        shelly_switch_id=shelly_switch_id,
        shelly_username=values.get("SHELLY_USERNAME"),
        shelly_password=values.get("SHELLY_PASSWORD"),
        shelly_use_https=shelly_use_https,
        shelly_timeout_seconds=shelly_timeout_seconds,
    )
