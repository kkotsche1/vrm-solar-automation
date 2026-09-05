"""record the daytime solar-surplus turn-on override

Revision ID: 20260905_0014
Revises: 20260904_0013
Create Date: 2026-09-05 09:40:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260905_0014"
down_revision = "20260904_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The daytime SOC gate can now be overridden by live solar surplus. Without these the
    # log cannot distinguish "ran because SOC was high enough" from "ran because the panels
    # were carrying it", which are the two cases a retune needs to tell apart.
    with op.batch_alter_table("control_cycle") as batch:
        batch.add_column(
            sa.Column(
                "daytime_surplus_override_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("daytime_projected_surplus_kw", sa.Float()))


def downgrade() -> None:
    with op.batch_alter_table("control_cycle") as batch:
        batch.drop_column("daytime_projected_surplus_kw")
        batch.drop_column("daytime_surplus_override_active")
