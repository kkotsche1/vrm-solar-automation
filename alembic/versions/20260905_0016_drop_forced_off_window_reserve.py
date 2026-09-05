"""drop the forced-off window reserve

Revision ID: 20260905_0016
Revises: 20260905_0015
Create Date: 2026-09-05 10:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260905_0016"
down_revision = "20260905_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The forced-off window is gone: the base-load reserve to solar crossover already
    # accounts for every hour left in the night, so a second overlapping reserve only
    # capped pump runtime without protecting anything the first one had missed.
    with op.batch_alter_table("control_cycle") as batch:
        batch.drop_column("night_forced_off_reserve_soc_percent")


def downgrade() -> None:
    with op.batch_alter_table("control_cycle") as batch:
        batch.add_column(sa.Column("night_forced_off_reserve_soc_percent", sa.Float()))
