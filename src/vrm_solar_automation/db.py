from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import BIGINT, BOOLEAN, FLOAT, INTEGER, TEXT, Index, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class ControllerStateRecord(Base):
    __tablename__ = "controller_state"

    id: Mapped[int] = mapped_column(INTEGER, primary_key=True, default=1)
    is_on: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)
    changed_at_iso: Mapped[str] = mapped_column(String(40), nullable=False)
    quiet_hours_forced_off: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    battery_alert_below_40_sent: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=False
    )
    battery_alert_below_35_sent: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=False
    )
    battery_alert_below_30_sent: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=False
    )
    generator_running_alert_sent: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=False
    )
    weather_cache_local_date: Mapped[str | None] = mapped_column(String(10))
    weather_cache_current_temperature_c: Mapped[float | None] = mapped_column(FLOAT)
    weather_cache_today_min_temperature_c: Mapped[float | None] = mapped_column(FLOAT)
    weather_cache_today_max_temperature_c: Mapped[float | None] = mapped_column(FLOAT)
    weather_cache_today_sunshine_hours: Mapped[float | None] = mapped_column(FLOAT)
    weather_cache_tomorrow_sunshine_hours: Mapped[float | None] = mapped_column(FLOAT)
    weather_cache_weather_code: Mapped[int | None] = mapped_column(INTEGER)
    weather_cache_queried_timezone: Mapped[str | None] = mapped_column(String(128))
    weather_cache_cached_at_iso: Mapped[str | None] = mapped_column(String(40))
    last_known_plug_is_on: Mapped[bool | None] = mapped_column(BOOLEAN)
    last_known_plug_at_iso: Mapped[str | None] = mapped_column(String(40))
    last_actuation_error: Mapped[str | None] = mapped_column(TEXT)
    last_actuation_at_iso: Mapped[str | None] = mapped_column(String(40))
    updated_at_iso: Mapped[str] = mapped_column(String(40), nullable=False)


class ControlCycleRecord(Base):
    __tablename__ = "control_cycle"

    id: Mapped[int] = mapped_column(INTEGER, primary_key=True, autoincrement=True)
    timestamp_unix_ms: Mapped[int] = mapped_column(BIGINT, nullable=False)
    timestamp_iso: Mapped[str] = mapped_column(String(40), nullable=False)

    site_id: Mapped[int] = mapped_column(INTEGER, nullable=False)
    site_name: Mapped[str] = mapped_column(String(255), nullable=False)
    site_identifier: Mapped[str] = mapped_column(String(255), nullable=False)

    power_queried_at_unix_ms: Mapped[int | None] = mapped_column(BIGINT)
    power_queried_at_iso: Mapped[str | None] = mapped_column(String(40))
    battery_soc_percent: Mapped[float | None] = mapped_column(FLOAT)
    solar_watts: Mapped[float | None] = mapped_column(FLOAT)
    house_watts: Mapped[float | None] = mapped_column(FLOAT)
    house_l1_watts: Mapped[float | None] = mapped_column(FLOAT)
    house_l2_watts: Mapped[float | None] = mapped_column(FLOAT)
    house_l3_watts: Mapped[float | None] = mapped_column(FLOAT)
    generator_watts: Mapped[float | None] = mapped_column(FLOAT)
    active_input_source: Mapped[int | None] = mapped_column(INTEGER)

    current_temperature_c: Mapped[float | None] = mapped_column(FLOAT)
    today_min_temperature_c: Mapped[float | None] = mapped_column(FLOAT)
    today_max_temperature_c: Mapped[float | None] = mapped_column(FLOAT)
    today_sunshine_hours: Mapped[float | None] = mapped_column(FLOAT)
    tomorrow_sunshine_hours: Mapped[float | None] = mapped_column(FLOAT)
    weather_code: Mapped[int | None] = mapped_column(INTEGER)
    queried_timezone: Mapped[str | None] = mapped_column(String(128))
    weather_source: Mapped[str] = mapped_column(String(32), nullable=False)

    should_turn_on: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)
    decision_action: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_reason: Mapped[str] = mapped_column(TEXT, nullable=False)
    decision_weather_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    night_required_soc_percent: Mapped[float | None] = mapped_column(FLOAT)
    night_surplus_mode_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)

    intended_target_is_on: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)
    quiet_hours_blocked: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(TEXT)

    actuation_status: Mapped[str] = mapped_column(String(64), nullable=False)
    actuation_command_sent: Mapped[str | None] = mapped_column(String(32))
    actuation_observed_before_is_on: Mapped[bool | None] = mapped_column(BOOLEAN)
    actuation_observed_after_is_on: Mapped[bool | None] = mapped_column(BOOLEAN)
    actuation_error: Mapped[str | None] = mapped_column(TEXT)


Index("ix_control_cycle_timestamp_unix_ms", ControlCycleRecord.timestamp_unix_ms)
Index(
    "ix_control_cycle_site_identifier_timestamp_unix_ms",
    ControlCycleRecord.site_identifier,
    ControlCycleRecord.timestamp_unix_ms,
)
Index(
    "ix_control_cycle_command_sent_timestamp_unix_ms",
    ControlCycleRecord.actuation_command_sent,
    ControlCycleRecord.timestamp_unix_ms,
)


def create_engine_for_url(database_url: str) -> Engine:
    _ensure_sqlite_parent_dir(database_url)
    engine = create_engine(database_url)
    if database_url.startswith("sqlite"):
        _configure_sqlite_pragmas(engine)
    return engine


def create_session_factory(database_url: str) -> sessionmaker:
    return sessionmaker(bind=create_engine_for_url(database_url), expire_on_commit=False)


def upgrade_database(database_url: str) -> None:
    config = _build_alembic_config(database_url)
    command.upgrade(config, "head")


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return

    path = Path(database_url.removeprefix("sqlite:///"))
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def _build_alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config
