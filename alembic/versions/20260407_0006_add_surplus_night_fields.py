"""add surplus-night forecast and diagnostics fields

Revision ID: 20260407_0006
Revises: 20260406_0005
Create Date: 2026-04-07 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260407_0006"
down_revision = "20260406_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controller_state",
        sa.Column("weather_cache_tomorrow_sunshine_hours", sa.Float(), nullable=True),
    )
    op.add_column(
        "control_cycle",
        sa.Column("tomorrow_sunshine_hours", sa.Float(), nullable=True),
    )
    op.add_column(
        "control_cycle",
        sa.Column("night_required_soc_percent", sa.Float(), nullable=True),
    )
    op.add_column(
        "control_cycle",
        sa.Column(
            "night_surplus_mode_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("control_cycle", "night_surplus_mode_active")
    op.drop_column("control_cycle", "night_required_soc_percent")
    op.drop_column("control_cycle", "tomorrow_sunshine_hours")
    op.drop_column("controller_state", "weather_cache_tomorrow_sunshine_hours")
