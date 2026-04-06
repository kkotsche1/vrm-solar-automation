"""add sunshine-hours tracking

Revision ID: 20260406_0005
Revises: 20260405_0004
Create Date: 2026-04-06 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260406_0005"
down_revision = "20260405_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_today_sunshine_hours", sa.Float(), nullable=True),
    )
    op.add_column(
        "control_cycle",
        sa.Column("today_sunshine_hours", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("control_cycle", "today_sunshine_hours")
    op.drop_column("controller_state", "weather_cache_today_sunshine_hours")
