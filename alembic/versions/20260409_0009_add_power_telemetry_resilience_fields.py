"""add power telemetry resilience fields

Revision ID: 20260409_0009
Revises: 20260408_0008
Create Date: 2026-04-09 19:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260409_0009"
down_revision = "20260408_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controller_state",
        sa.Column(
            "consecutive_power_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "controller_state",
        sa.Column("last_power_failure_at_iso", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "controller_state",
        sa.Column("last_power_failure_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "control_cycle",
        sa.Column(
            "power_status_source",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "control_cycle",
        sa.Column(
            "power_status_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "control_cycle",
        sa.Column("power_status_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("control_cycle", "power_status_error")
    op.drop_column("control_cycle", "power_status_available")
    op.drop_column("control_cycle", "power_status_source")
    op.drop_column("controller_state", "last_power_failure_error")
    op.drop_column("controller_state", "last_power_failure_at_iso")
    op.drop_column("controller_state", "consecutive_power_failures")
