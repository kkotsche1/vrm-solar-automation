"""add plug-mismatch alert latch to controller state

Revision ID: 20260420_0010
Revises: 20260409_0009
Create Date: 2026-04-20 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260420_0010"
down_revision = "20260409_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controller_state",
        sa.Column(
            "plug_mismatch_alert_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("controller_state", "plug_mismatch_alert_sent")
