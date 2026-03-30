from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

def setup_database(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with closing(sqlite3.connect(path)) as conn:
        # Enable Write-Ahead Logging for better concurrent read/write and less SD wear
        conn.execute("PRAGMA journal_mode=WAL;")
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                timestamp_unix_ms INTEGER PRIMARY KEY,
                timestamp_iso TEXT NOT NULL,
                battery_soc_percent REAL,
                solar_watts REAL,
                house_watts REAL,
                generator_watts REAL,
                active_input_source INTEGER,
                current_temperature_c REAL,
                pump_is_on INTEGER,
                override_active INTEGER,
                raw_payload TEXT
            )
        """)
        conn.commit()

def insert_metrics(db_path: str | Path, payload: dict[str, object]) -> None:
    path = Path(db_path)
    
    try:
        control_loop = payload.get("control_loop", {})
        power = payload.get("power", {})
        weather = payload.get("weather", {})
        next_state = payload.get("next_state", {})
        override = payload.get("override", {})
        
        # Use queried_at_unix_ms from power, or fallback to now
        power_unix = power.get("queried_at_unix_ms")
        timestamp_unix_ms = int(power_unix) if power_unix is not None else int(datetime.now(UTC).timestamp() * 1000)
        
        # Use the control loop's last completed time, or fallback to now
        timestamp_iso = control_loop.get("last_completed_at_iso")
        if not timestamp_iso:
            timestamp_iso = datetime.now(UTC).isoformat()
        
        battery_soc_percent = power.get("battery_soc_percent")
        solar_watts = power.get("solar_watts")
        house_watts = power.get("house_watts")
        generator_watts = power.get("generator_watts")
        active_input_source = power.get("active_input_source")
        
        current_temperature_c = weather.get("current_temperature_c")
        
        pump_is_on = 1 if next_state.get("is_on") else 0
        override_active = 1 if override.get("is_active") else 0
        
        raw_payload = json.dumps(payload, sort_keys=True)
        
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO metrics (
                    timestamp_unix_ms,
                    timestamp_iso,
                    battery_soc_percent,
                    solar_watts,
                    house_watts,
                    generator_watts,
                    active_input_source,
                    current_temperature_c,
                    pump_is_on,
                    override_active,
                    raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp_unix_ms,
                timestamp_iso,
                battery_soc_percent,
                solar_watts,
                house_watts,
                generator_watts,
                active_input_source,
                current_temperature_c,
                pump_is_on,
                override_active,
                raw_payload
            ))
            conn.commit()
            
    except Exception:
        logger.exception("Failed to insert metrics into database")
