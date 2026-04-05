"""add quiet-hours flag to controller state

Revision ID: 20260405_0002
Revises: 20260405_0001
Create Date: 2026-04-05 00:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260405_0002"
down_revision = "20260405_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controller_state",
        sa.Column(
            "quiet_hours_forced_off",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("controller_state", "quiet_hours_forced_off")
