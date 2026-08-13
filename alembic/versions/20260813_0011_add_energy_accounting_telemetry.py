"""add battery power, per-cycle plug state, and policy fingerprint to control cycles

Revision ID: 20260813_0011
Revises: 20260420_0010
Create Date: 2026-08-13 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260813_0011"
down_revision = "20260420_0010"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("battery_power_w", sa.Float),
    ("plug_observed_is_on", sa.Boolean),
    ("policy_battery_capacity_kwh", sa.Float),
    ("policy_night_base_load_kw", sa.Float),
    ("policy_battery_hard_min_soc_percent", sa.Float),
)


def upgrade() -> None:
    for name, column_type in _COLUMNS:
        op.add_column("control_cycle", sa.Column(name, column_type(), nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("control_cycle", name)
