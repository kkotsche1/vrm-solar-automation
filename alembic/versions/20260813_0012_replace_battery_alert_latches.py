"""replace fixed battery alert latches with a configurable latched-percent list

Revision ID: 20260813_0012
Revises: 20260813_0011
Create Date: 2026-08-13 14:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260813_0012"
down_revision = "20260813_0011"
branch_labels = None
depends_on = None

_LEGACY_COLUMNS = (
    "battery_alert_below_40_sent",
    "battery_alert_below_35_sent",
    "battery_alert_below_30_sent",
)


def upgrade() -> None:
    with op.batch_alter_table("controller_state") as batch:
        batch.add_column(sa.Column("battery_alert_latched_percents", sa.String(length=128)))
        for column in _LEGACY_COLUMNS:
            batch.drop_column(column)


def downgrade() -> None:
    with op.batch_alter_table("controller_state") as batch:
        for column in _LEGACY_COLUMNS:
            batch.add_column(
                sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false())
            )
        batch.drop_column("battery_alert_latched_percents")
