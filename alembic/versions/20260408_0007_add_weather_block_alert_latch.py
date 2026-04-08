"""add weather-block alert latch to controller state

Revision ID: 20260408_0007
Revises: 20260407_0006
Create Date: 2026-04-08 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260408_0007"
down_revision = "20260407_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controller_state",
        sa.Column("weather_block_alert_sent_local_date", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("controller_state", "weather_block_alert_sent_local_date")
