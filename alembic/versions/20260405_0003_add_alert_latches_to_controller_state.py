"""add alert latches to controller state

Revision ID: 20260405_0003
Revises: 20260405_0002
Create Date: 2026-04-05 01:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260405_0003"
down_revision = "20260405_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controller_state",
        sa.Column(
            "battery_alert_below_40_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "controller_state",
        sa.Column(
            "battery_alert_below_35_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "controller_state",
        sa.Column(
            "battery_alert_below_30_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "controller_state",
        sa.Column(
            "generator_running_alert_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("controller_state", "generator_running_alert_sent")
    op.drop_column("controller_state", "battery_alert_below_30_sent")
    op.drop_column("controller_state", "battery_alert_below_35_sent")
    op.drop_column("controller_state", "battery_alert_below_40_sent")
