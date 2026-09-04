"""cache sunrise/sunset and record the resolved solar crossover

Revision ID: 20260904_0013
Revises: 20260813_0012
Create Date: 2026-09-04 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260904_0013"
down_revision = "20260813_0012"
branch_labels = None
depends_on = None

_STATE_COLUMNS = (
    "weather_cache_today_sunrise_iso",
    "weather_cache_tomorrow_sunrise_iso",
    "weather_cache_today_sunset_iso",
    "weather_cache_tomorrow_sunset_iso",
)


def upgrade() -> None:
    # The overnight reserve is budgeted to solar crossover, which is derived from sunrise.
    # Without these the same-day cache used when the forecast fetch fails would drop the
    # sunrise and fall back to the fixed crossover time — exactly overnight, when it matters.
    with op.batch_alter_table("controller_state") as batch:
        for column in _STATE_COLUMNS:
            batch.add_column(sa.Column(column, sa.String(length=40)))
    with op.batch_alter_table("control_cycle") as batch:
        batch.add_column(sa.Column("night_solar_crossover_local", sa.String(length=5)))
        batch.add_column(sa.Column("night_solar_crossover_source", sa.String(length=16)))


def downgrade() -> None:
    with op.batch_alter_table("control_cycle") as batch:
        batch.drop_column("night_solar_crossover_source")
        batch.drop_column("night_solar_crossover_local")
    with op.batch_alter_table("controller_state") as batch:
        for column in _STATE_COLUMNS:
            batch.drop_column(column)
