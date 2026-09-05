"""record the generator start guard and the forced-off window reserve

Revision ID: 20260905_0015
Revises: 20260905_0014
Create Date: 2026-09-05 10:05:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260905_0015"
down_revision = "20260905_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Two new ways a cycle can end up OFF that the reason text alone cannot be queried for:
    # a start refused because the generator was running, and the forced-off window's own
    # pump-draw reserve. Both need to be countable to tell whether they are earning their keep.
    with op.batch_alter_table("control_cycle") as batch:
        batch.add_column(
            sa.Column(
                "generator_start_blocked",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("night_forced_off_reserve_soc_percent", sa.Float()))


def downgrade() -> None:
    with op.batch_alter_table("control_cycle") as batch:
        batch.drop_column("night_forced_off_reserve_soc_percent")
        batch.drop_column("generator_start_blocked")
