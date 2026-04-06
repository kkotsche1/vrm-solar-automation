"""add weather cache and source tracking

Revision ID: 20260405_0004
Revises: 20260405_0003
Create Date: 2026-04-05 02:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260405_0004"
down_revision = "20260405_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_local_date", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_current_temperature_c", sa.Float(), nullable=True),
    )
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_today_min_temperature_c", sa.Float(), nullable=True),
    )
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_today_max_temperature_c", sa.Float(), nullable=True),
    )
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_weather_code", sa.Integer(), nullable=True),
    )
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_queried_timezone", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_cached_at_iso", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "control_cycle",
        sa.Column(
            "weather_source",
            sa.String(length=32),
            nullable=False,
            server_default="legacy",
        ),
    )


def downgrade() -> None:
    op.drop_column("control_cycle", "weather_source")
    op.drop_column("controller_state", "weather_cache_cached_at_iso")
    op.drop_column("controller_state", "weather_cache_queried_timezone")
    op.drop_column("controller_state", "weather_cache_weather_code")
    op.drop_column("controller_state", "weather_cache_today_max_temperature_c")
    op.drop_column("controller_state", "weather_cache_today_min_temperature_c")
    op.drop_column("controller_state", "weather_cache_current_temperature_c")
    op.drop_column("controller_state", "weather_cache_local_date")
