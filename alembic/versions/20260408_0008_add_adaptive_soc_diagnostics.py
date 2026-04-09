"""add adaptive SOC diagnostics to control cycles

Revision ID: 20260408_0008
Revises: 20260408_0007
Create Date: 2026-04-08 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260408_0008"
down_revision = "20260408_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "control_cycle",
        sa.Column(
            "soc_control_mode",
            sa.String(length=32),
            nullable=False,
            server_default="daytime_adaptive",
        ),
    )
    op.add_column(
        "control_cycle",
        sa.Column("effective_turn_on_soc_percent", sa.Float(), nullable=True),
    )
    op.add_column(
        "control_cycle",
        sa.Column("effective_turn_off_soc_percent", sa.Float(), nullable=True),
    )
    op.add_column(
        "control_cycle",
        sa.Column("forecast_liberal_factor", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("control_cycle", "forecast_liberal_factor")
    op.drop_column("control_cycle", "effective_turn_off_soc_percent")
    op.drop_column("control_cycle", "effective_turn_on_soc_percent")
    op.drop_column("control_cycle", "soc_control_mode")
